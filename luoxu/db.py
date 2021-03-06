import logging
import contextlib
from typing import Literal
import asyncio
from random import randint

import asyncpg

from .util import format_name, UpdateLoaded
from .indexing import text_to_query, format_msg
from .types import SearchQuery, GroupNotFound
from .ctxvars import msg_source

logger = logging.getLogger(__name__)

class PostgreStore:
  SEARCH_LIMIT = 50

  def __init__(self, address: str) -> None:
    self.address = address
    self.pool = None

  async def setup(self) -> None:
    self.pool = await asyncpg.create_pool(self.address)

  async def _insert_one_message(self, conn, msg, text):
    u = msg.sender
    sql = '''
      INSERT INTO messages (group_id, msgid, from_user, from_user_name, text, created_at, updated_at)
      VALUES ($1, $2, $3, $4, $5, $6, $7)
      ON CONFLICT (msgid, group_id) DO UPDATE
        SET text = EXCLUDED.text, updated_at = EXCLUDED.updated_at
    '''
    logger.info('%7s <%s> [%s] %s: %s', msg_source.get(), msg.chat.title, msg.id, format_name(u), text)
    await conn.execute(sql,
      msg.peer_id.channel_id,
      msg.id,
      u.id if u else None,
      format_name(u),
      text,
      msg.date,
      msg.edit_date,
    )

  async def insert_messages(self, msgs, update_loaded):
    data = [(msg, text) for msg in msgs
            if (text := format_msg(msg)) is not None]
    if not data:
      return

    while True:
      try:
        async with self.get_conn() as conn:
          for msg, text in data:
            await self._insert_one_message(conn, msg, text)
          if update_loaded in [UpdateLoaded.update_last, UpdateLoaded.update_both]:
            await self.loaded_upto(conn, msg.peer_id.channel_id, 1, msgs[-1].id)
          if update_loaded in [UpdateLoaded.update_first, UpdateLoaded.update_both]:
            await self.loaded_upto(conn, msg.peer_id.channel_id, -1, msgs[0].id)
          break
      except asyncpg.exceptions.DeadlockDetectedError:
        t = randint(1, 50) / 10
        logger.warning('deadlock detected, retry in %.1fs', t)
        await asyncio.sleep(t)

  async def get_group(self, conn, group_id: int):
    sql = '''\
        select * from tg_groups
        where group_id = $1'''
    return await conn.fetchrow(sql, group_id)

  async def insert_group(self, conn, group):
    g = await self.get_group(conn, group.id)
    if g:
      return g

    sql = '''\
        insert into tg_groups
        (group_id, name, pub_id) values
        ($1,       $2,  $3)
        returning *'''
    return await conn.fetchrow(
      sql,
      group.id,
      group.title,
      group.username,
    )

  async def loaded_upto(
    self, conn, group_id: int,
    direction: Literal[1, -1], msgid: int,
  ) -> None:
    sql = '''update tg_groups set %s = $1 where group_id = $2''' % ({
      1: 'loaded_last_id', -1: 'loaded_first_id',
    }[direction])
    await conn.execute(sql, msgid, group_id)

  @contextlib.asynccontextmanager
  async def get_conn(self):
    async with self.pool.acquire() as conn, conn.transaction():
      yield conn

  async def search(self, q: SearchQuery) -> list[dict]:
    async with self.get_conn() as conn:
      if q.group:
        group = await self.get_group(conn, q.group)
        if not group:
          raise GroupNotFound(q.group)
        groupinfo = {
          q.group: [group['pub_id'], group['name']],
        }
      else:
        sql = '''select group_id, pub_id, name from tg_groups'''
        rows = await conn.fetch(sql)
        groupinfo = {row['group_id']: [row['pub_id'], row['name']] for row in rows}

      # run a subquery to highlight because it would highlight all
      # matched rows (ignoring limits) otherwise
      common_cols = 'msgid, group_id, from_user, from_user_name, created_at, updated_at'
      sql = '''select {0}, text from messages where 1 = 1'''
      highlight = None
      params = []
      if q.group:
        sql += f''' and group_id = ${len(params)+1}'''
        params.append(q.group)
      if q.terms:
        query = text_to_query(q.terms.strip())
        if not query:
          raise ValueError
        sql += f''' and text &@~ ${len(params)+1}'''
        params.append(query)
        highlight = f'''pgroonga_highlight_html(text, pgroonga_query_extract_keywords(${len(params)+1}), 'message_idx') as html'''
        params.append(query)
      if q.sender:
        sql += f''' and from_user = ${len(params)+1}'''
        params.append(q.sender)
      if q.start:
        sql += f''' and created_at > ${len(params)+1}'''
        params.append(q.start)
      if q.end:
        sql += f''' and created_at < ${len(params)+1}'''
        params.append(q.end)

      sql += f' order by created_at desc limit {self.SEARCH_LIMIT}'
      if highlight:
        sql = f'select {{0}}, {highlight} from ({sql}) as t'
      sql = sql.format(common_cols)
      logger.debug('searching: %s: %s', sql, params)
      rows = await conn.fetch(sql, *params)
      return groupinfo, rows

  async def get_groups(self):
    async with self.get_conn() as conn:
      sql = '''select * from tg_groups'''
      return await conn.fetch(sql)

  async def find_names(self, group: int, q: str) -> list[tuple[str, str]]:
    q = q.strip()
    if not q:
      raise ValueError
    async with self.get_conn() as conn:
      if group:
        gq = ' and $2 = ANY (group_id)'
        args = (q, group)
      else:
        gq = ''
        args = (q,)
      sql = f'''\
        select name, uid from usernames
        where name &@ $1{gq}
        order by last_seen desc
        limit 15;
      '''
      return [(uid, r['name'])
              for r in await conn.fetch(sql, *args)
              for uid in r['uid']]

