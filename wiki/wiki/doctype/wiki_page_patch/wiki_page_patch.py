# Copyright (c) 2021, Frappe and contributors
# For license information, please see license.txt


import json

import frappe
from frappe import _
from frappe.desk.form.utils import add_comment
from frappe.model.document import Document
from frappe.website.utils import cleanup_page_name
from ghdiff import diff


class WikiPagePatch(Document):
	def validate(self):
		self.new_preview_store = frappe.utils.md_to_html(self.new_code)
		if not self.new:
			self.orignal_code = frappe.db.get_value("Wiki Page", self.wiki_page, "content")
			self.diff = diff(self.orignal_code, self.new_code)
			self.orignal_preview_store = frappe.utils.md_to_html(self.orignal_code)

	def after_insert(self):
		add_comment_to_patch(self.name, self.message)
		frappe.db.commit()

	def on_update(self):
		# A suggestion waiting for review tells the approvers once — on creation, or
		# when a draft is sent for review. An approver's own edit is approved in the
		# same request, so it never pings anyone.
		if self.docstatus != 0 or self.status != "Under Review":
			return
		if not (self.flags.in_insert or self.has_value_changed("status")):
			return
		if frappe.has_permission("Wiki Page Patch", ptype="submit", user=self.raised_by, throw=False):
			return
		page = frappe.db.get_value("Wiki Page", self.wiki_page, "title") or self.wiki_page
		_notify(_wiki_approvers(), _("Wiki suggestion to review: {0} (by {1})").format(
			page if not self.new else (self.new_title or page), frappe.utils.get_fullname(self.raised_by)), self)

	def on_submit_notify(self):
		if not self.raised_by or self.raised_by == self.approved_by:
			return
		page = self.new_title or frappe.db.get_value("Wiki Page", self.wiki_page, "title") or self.wiki_page
		verdict = _("approved and published") if self.status == "Approved" else _("not accepted")
		_notify([self.raised_by], _("Your wiki suggestion for {0} was {1}").format(page, verdict), self)

	def on_submit(self):
		self.on_submit_notify()
		if self.status == "Rejected":
			return

		if self.status != "Approved":
			frappe.throw(_("Please approve/ reject the request before submitting"))

		self.wiki_page_doc = frappe.get_doc("Wiki Page", self.wiki_page)

		self.clear_sidebar_cache()

		if self.new:
			self.create_new_wiki_page()
			self.update_sidebars()
		else:
			self.update_old_page()

	def clear_sidebar_cache(self):
		if self.new or self.new_title != self.wiki_page_doc.title:
			for key in frappe.cache().hgetall("wiki_sidebar").keys():
				frappe.cache().hdel("wiki_sidebar", key)

	def create_new_wiki_page(self):
		self.new_wiki_page = frappe.new_doc("Wiki Page")

		wiki_page_dict = {
			"title": self.new_title,
			"content": self.new_code,
			"route": "/".join(
				self.wiki_page_doc.route.split("/")[:-1] + [cleanup_page_name(self.new_title)]
			),
			"published": 1,
			"allow_guest": self.wiki_page_doc.allow_guest,
		}

		self.new_wiki_page.update(wiki_page_dict)
		self.new_wiki_page.save()

	def update_old_page(self):
		self.wiki_page_doc.update_page(self.new_title, self.new_code, self.message, self.raised_by)

	def update_sidebars(self):
		if not self.new_sidebar_items:
			self.new_sidebar_items = "{}"

		sidebars = json.loads(self.new_sidebar_items)

		sidebar_items = sidebars.items()
		if sidebar_items:
			idx = 0
			for sidebar, items in sidebar_items:
				for item in items:
					idx += 1
					if item["name"] == "new-wiki-page":
						item["name"] = self.new_wiki_page.name
						# AddisFly: the space is the CLOSEST parent folder that is a
						# Wiki Space. Looking up only the immediate parent failed for every
						# page in a sub-folder (1,343 of 1,453): "Wiki Space None not found".
						wiki_space_name = None
						parts = self.wiki_page_doc.route.split("/")[:-1]
						while parts and not wiki_space_name:
							wiki_space_name = frappe.get_value("Wiki Space", {"route": "/".join(parts)})
							parts.pop()
						if not wiki_space_name:
							frappe.throw(_("No Wiki Space found for {0}").format(self.wiki_page_doc.route))

						wiki_space = frappe.get_doc("Wiki Space", wiki_space_name)
						wiki_space.append(
							"wiki_sidebars",
							{
								"wiki_page": self.new_wiki_page.name,
								"parent_label": list(sidebars)[-1],
							},
						)
						wiki_space.save()

					frappe.db.set_value(
						"Wiki Group Item", {"wiki_page": str(item["name"])}, {"parent_label": sidebar, "idx": idx}
					)


def _wiki_approvers():
	"""Enabled users holding Wiki Approver (System Managers if there are none)."""
	def holders(role):
		return frappe.db.sql_list(
			"""SELECT DISTINCT u.name FROM `tabUser` u JOIN `tabHas Role` r ON r.parent = u.name
			   AND r.parenttype = 'User' WHERE r.role = %s AND u.enabled = 1
			   AND u.name NOT IN ('Administrator', 'Guest')""", role)
	return holders("Wiki Approver") or holders("System Manager")


def _notify(users, subject, doc):
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	users = sorted({u for u in users if u and u != frappe.session.user})
	if users:
		enqueue_create_notification(users, {
			"type": "Alert", "document_type": doc.doctype, "document_name": doc.name,
			"subject": subject, "from_user": frappe.session.user})


@frappe.whitelist()
def add_comment_to_patch(reference_name, content):
	# AddisFly: anyone logged in could comment on any suggestion
	frappe.get_doc("Wiki Page Patch", reference_name).check_permission("read")
	email = frappe.session.user
	name = frappe.db.get_value("User", frappe.session.user, ["first_name"], as_dict=True).get(
		"first_name"
	)
	comment = add_comment("Wiki Page Patch", reference_name, content, email, name)
	comment.timepassed = frappe.utils.pretty_date(comment.creation)
	return comment
