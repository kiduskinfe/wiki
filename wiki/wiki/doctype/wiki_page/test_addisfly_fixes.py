# Copyright (c) 2026, AddisFly
"""AddisFly fork fixes (2026-10-08): title printed twice, and new pages that could
not be created from inside a sub-folder."""

import json
import unittest

import frappe

from wiki.wiki.doctype.wiki_page.wiki_page import strip_leading_title


class TestTheTitleIsPrintedOnce(unittest.TestCase):
	def test_a_leading_copy_of_the_title_is_dropped(self):
		self.assertEqual(strip_leading_title("# Refund Policy\n\nBody", "Refund Policy"), "Body")

	def test_case_spacing_and_ampersands_do_not_matter(self):
		self.assertEqual(
			strip_leading_title("#  Hiring to Day 90 — Recruitment &amp; Onboarding SOP\nx",
			                    "Hiring to Day 90 — Recruitment & Onboarding SOP"), "x")

	def test_a_different_first_heading_stays(self):
		text = "# Overview\n\nBody"
		self.assertEqual(strip_leading_title(text, "Refund Policy"), text)

	def test_no_heading_is_untouched(self):
		self.assertEqual(strip_leading_title("Body", "T"), "Body")
		self.assertEqual(strip_leading_title("## T\nBody", "T"), "## T\nBody")
		self.assertEqual(strip_leading_title(None, "T"), None)


class TestANewPageCanBeAddedFromASubFolder(unittest.TestCase):
	"""1,343 of 1,453 pages live in a sub-folder (operations-manual/hr/...). Adding a
	page from one of them failed with "Wiki Space None not found"."""

	def setUp(self):
		self._commit = frappe.db.commit
		frappe.db.commit = lambda *a, **k: None
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.commit = self._commit

	def test_new_page_beside_a_nested_page(self):
		from wiki.wiki.doctype.wiki_page.wiki_page import update

		nested = frappe.db.get_value("Wiki Page", {"route": ["like", "%/%/%"], "published": 1},
		                             ["name", "route"], as_dict=True)
		if not nested:
			self.skipTest("no nested page")
		out = update(name=nested.name, content="ZZ test body", title="ZZ Nested Test Page", new=True,
		             new_sidebar_items=json.dumps({"ZZ": [{"name": "new-wiki-page", "title": "ZZ"}]}))
		self.assertTrue(out.get("approved"))
		page = frappe.db.get_value("Wiki Page", {"title": "ZZ Nested Test Page"}, ["name", "route"], as_dict=True)
		self.assertTrue(page.route.startswith(nested.route.rsplit("/", 1)[0] + "/"))
		self.assertTrue(frappe.db.exists("Wiki Group Item", {"wiki_page": page.name}),
		                "the new page must be in its space's sidebar or nobody can reach it")
