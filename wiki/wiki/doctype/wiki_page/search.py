# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt


import frappe
from frappe.utils import strip_html_tags, update_progress_bar
from frappe.utils.redis_wrapper import RedisWrapper
from redis.exceptions import ResponseError

# ⚠️ `redisearch` (the standalone client) HARD-PINS `redis==3.5.3`, and frappe
# requires `redis~=4.5.5`. Listing it in requirements.txt therefore downgrades
# redis-py on every `bench setup requirements` and takes the site down with
# "cannot import name ... from redis" — that is exactly what caused the 502 on
# 2026-10-12. It is also dead weight here: RediSearch is an optional Redis
# SERVER module, and this host does not load it (`FT.SEARCH` is an unknown
# command), so the index path can never succeed anyway.
#
# So the dependency is gone from requirements.txt and the import is optional.
# Every entry point below degrades to the plain-SQL `_db_search` / to a no-op.
# To actually use RediSearch you need BOTH a redis build with the module AND a
# client that works on redis-py 4 (`redis.commands.search`, not this package).
try:
	from redisearch import Client, IndexDefinition, Query, TextField
except ImportError:
	Client = IndexDefinition = Query = TextField = None

PREFIX = "wiki_page_search_doc"

# None = not yet probed in this process. A `bench restart` re-probes, which is
# the right granularity for something that only changes when redis is rebuilt.
_SERVER_HAS_REDISEARCH = None


def redisearch_available():
	"""True only when the client library AND the Redis server module are present.

	Checked with `COMMAND INFO FT.SEARCH` rather than `MODULE LIST`, because
	MODULE LIST is non-empty whenever ANY module is loaded (RedisJSON, say) and
	would be a false positive.
	"""
	global _SERVER_HAS_REDISEARCH

	if Client is None:
		return False
	if _SERVER_HAS_REDISEARCH is None:
		try:
			info = frappe.cache().execute_command("COMMAND", "INFO", "FT.SEARCH")
			_SERVER_HAS_REDISEARCH = bool(info and info[0])
		except Exception:
			_SERVER_HAS_REDISEARCH = False
	return _SERVER_HAS_REDISEARCH


@frappe.whitelist(allow_guest=True)
def search(query, path, space):
	if not space:
		space = get_space_route(path)
	# RediSearch is an optional Redis module that is not installed on every host
	# (AddisFly fork). Try it when it is really there, but always fall back to a
	# database search so wiki search works regardless.
	if not redisearch_available():
		return _readable_only(_db_search(query, space))
	try:
		return _readable_only(_redisearch(query, space))
	except Exception:
		return _readable_only(_db_search(query, space))


def _readable_only(result, limit=10):
	"""Drop results the caller may not open (AddisFly fork).

	This endpoint is guest-callable and returned title + excerpt for EVERY page:
	anyone on the internet could search the login-only Operations Manual and the
	Board space. Results now pass the same rule as opening the page."""
	from wiki.wiki.doctype.wiki_page.wiki_page import page_readable

	docs = result.get("docs") or []
	guest = {
		d.name: d.allow_guest
		for d in frappe.get_all(
			"Wiki Page", filters={"name": ["in", [d.name for d in docs] or [""]]}, fields=["name", "allow_guest"]
		)
	}
	kept = [d for d in docs if page_readable(d.name, d.route, guest.get(d.name))][:limit]
	result["docs"] = kept
	result["total"] = len(kept)
	return result


def _db_search(query, space):
	import time

	t0 = time.monotonic()
	q = (query or "").strip()
	if not q:
		return {"docs": [], "total": 0, "duration": 0}
	like = f"%{q}%"
	route_filter = f"{space}/%" if space else "%"
	rows = frappe.db.sql(
		"""
		SELECT name, title, route, content FROM `tabWiki Page`
		WHERE published = 1 AND route LIKE %(space)s
		  AND (title LIKE %(q)s OR content LIKE %(q)s)
		ORDER BY (title LIKE %(q)s) DESC, modified DESC
		LIMIT 40
		""",
		{"space": route_filter, "q": like},
		as_dict=True,
	)
	for d in rows:
		text = strip_html_tags(d.content or "")
		idx = text.lower().find(q.lower())
		if idx > 60:
			snippet = "..." + text[idx - 40 : idx + 140]
		else:
			snippet = text[:180]
		# light highlight
		try:
			import re

			snippet = re.sub(re.escape(q), lambda m: f"<mark>{m.group(0)}</mark>", snippet, flags=re.I)
		except Exception:
			pass
		d.content = snippet
	return {"docs": rows, "total": len(rows), "duration": round((time.monotonic() - t0) * 1000)}


def _redisearch(query, space):
	r = frappe.cache()

	client = Client(make_key(space), conn=r)

	query = Query(query).paging(0, 5).highlight(tags=["<mark>", "</mark>"])

	result = client.search(query)

	names = []
	for d in result.docs:
		_, name = d.id.split(":")
		names.append(name)
	names = list(set(names))

	data_by_name = {
		d.name: d
		for d in frappe.db.get_all("Wiki Page", fields=["name"], filters={"name": ["in", names]})
	}

	docs = []
	for d in result.docs:
		_, name = d.id.split(":")
		doc = data_by_name[name]
		doc.title = d.title
		doc.route = d.route
		doc.content = d.content
		docs.append(doc)

	return {"docs": docs, "total": result.total, "duration": result.duration}


def get_space_route(path):
	# AddisFly: the space is the longest space route the path STARTS with. A plain
	# substring test sent "operations-manual/operations/reports-dashboards-guide"
	# to the Board space ("board" is inside "dashboards") whenever that space
	# happened to be listed first.
	path = (path or "").strip("/")
	matches = [
		space
		for space in frappe.db.get_all("Wiki Space", pluck="route")
		if space and (path == space or path.startswith(space + "/"))
	]
	return max(matches, key=len) if matches else None


def rebuild_index():
	if not redisearch_available():
		return

	r = frappe.cache()
	r.set_value("wiki_page_index_in_progress", True)

	# Options for index creation
	schema = (
		TextField("title", weight=3.0),
		TextField("content"),
	)

	# Create an index and pass in the schema
	spaces = frappe.db.get_all("Wiki Space", pluck="route")
	wiki_pages = frappe.db.get_all("Wiki Page", fields=["name", "title", "content", "route"])
	for space in spaces:
		try:
			drop_index(space)

			index_def = IndexDefinition(
				prefix=[f"{r.make_key(f'{PREFIX}{space}').decode()}:"], score=0.5, score_field="doc_score"
			)

			index = Client(make_key(space), conn=frappe.cache())
			index.create_index(schema, definition=index_def)

			records_to_index = [d for d in wiki_pages if space in d.get("route")]
			create_index_for_records(records_to_index, space)
		except ResponseError as e:
			print(e)

	r.set_value("wiki_page_index_in_progress", False)


def rebuild_index_in_background():
	if not redisearch_available():
		return

	if not frappe.cache().get_value("wiki_page_index_in_progress"):
		print(f"Queued rebuilding of search index for {frappe.local.site}")
		frappe.enqueue(rebuild_index, queue="long")


def rebuild_index_if_not_exists():
	# Runs on the `all` scheduler (~every 4 min). Without this guard it refetched
	# every Wiki Page's content and fired doomed FT.CREATEs ~360 times a day.
	if not redisearch_available():
		return

	spaces = frappe.db.get_all("Wiki Space", pluck="route")
	for space in spaces:
		try:
			client = Client(make_key(space), conn=frappe.cache())
			client.info()
		except ResponseError:
			rebuild_index()
			break


def create_index_for_records(records, space):
	if not redisearch_available():
		return

	r = frappe.cache()
	for i, d in enumerate(records):
		if not hasattr(frappe.local, "request") and len(records) > 10:
			update_progress_bar(f"Indexing Wiki Pages - {space}", i, len(records), absolute=True)

		key = r.make_key(f"{PREFIX}{space}:{d.name}").decode()
		mapping = {
			"title": d.title,
			"content": strip_html_tags(d.content),
			"route": d.route,
		}
		super(RedisWrapper, r).hset(key, mapping=mapping)


def remove_index_for_records(records, space):
	if not redisearch_available():
		return

	r = frappe.cache()
	for d in records:
		try:
			key = r.make_key(f"{PREFIX}{space}:{d.name}").decode()
			client = Client(make_key(space), conn=frappe.cache())
			client.delete_document(key)
		except ResponseError:
			pass


def update_index(doc):
	record = frappe._dict(
		{"name": doc.name, "title": doc.title, "content": doc.content, "route": doc.route}
	)
	space = get_space_route(doc.route)

	create_index_for_records([record], space)


def remove_index(doc):
	record = frappe._dict(
		{
			"name": doc.name,
			"route": doc.route,
		}
	)
	space = get_space_route(doc.route)

	remove_index_for_records([record], space)


def drop_index(space):
	if not redisearch_available():
		return

	try:
		client = Client(make_key(space), conn=frappe.cache())
		client.drop_index(delete_documents=True)
	except ResponseError:
		pass


def make_key(key):
	return f"{frappe.conf.db_name}|{key}".encode()
