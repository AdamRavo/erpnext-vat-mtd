# -*- coding: utf-8 -*-
# Copyright (c) 2020 Software to Hardware Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document
from frappe.utils import get_url, nowdate, date_diff, flt
import json
import uk_vat.uk_vat_return.hmrc_api.vat as vat_api
import datetime

vat_return_schema = {
									# Box Number, Source of value, Description, Precision
	"vatDueSales":					[1, "vat", "VAT due on sales", 2],
	"vatDueAcquisitions":			[2, "vat", "VAT due on EU acquisitions", 2],
	"totalVatDue":					[3, None, "Total VAT due", 2],
	"vatReclaimedCurrPeriod":		[4, "vat", "VAT reclaimed on purchase", 2],
	"netVatDue": 					[5, None, "Net VAT", 2],
	"totalValueSalesExVAT": 		[6, "base_amount", "Total sales, ex. VAT", 0],
	"totalValuePurchasesExVAT": 	[7, "base_amount", "Total purchases, ex. VAT", 0],
	"totalValueGoodsSuppliedExVAT": [8, "base_amount", "Total EC supply of goods, ex VAT", 0],
	"totalAcquisitionsExVAT": 		[9, "base_amount", "Total EC acquisitions of goods, ex. VAT", 0]
}

# Fields on the UK VAT Return document that hold each HMRC box.
box_fields = {
	"vatDueSales":					"vat_output",
	"vatDueAcquisitions":			"vat_eu_acquisitions",
	"totalVatDue":					"vat_due_total",
	"vatReclaimedCurrPeriod":		"vat_input",
	"netVatDue":					"vat_net",
	"totalValueSalesExVAT":			"total_output_exvat",
	"totalValuePurchasesExVAT":		"total_input_exvat",
	"totalValueGoodsSuppliedExVAT":	"total_ec_goods_output",
	"totalAcquisitionsExVAT":		"total_ec_goods_input"
}

# Errors on earlier returns can be corrected on the next return when their
# net value is within these limits (VAT Notice 700/45, section 4). Larger
# errors have to be reported to HMRC separately.
ERROR_CORRECTION_LIMIT = 10000
ERROR_CORRECTION_CEILING = 50000


class UKVATReturn(Document):

	def validate(self):
		self.validate_adjustments()

	def before_save(self):

		vat_return = calculate_vat_return(self)

		for box, fieldname in box_fields.items():
			self.set(fieldname, vat_return[box])

		self.check_error_correction_limit()
		warn_lines_without_template(self.period_start_date,
									self.period_end_date)

	def validate_adjustments(self):

		box1 = flt(self.get("vat_output_adjustment"), 2)
		box4 = flt(self.get("vat_input_adjustment"), 2)

		if box1 < 0 or box4 < 0:
			frappe.throw("Adjustments must be positive. Put VAT you under-"
				"declared in the Box 1 adjustment and VAT you over-declared "
				"(or under-claimed) in the Box 4 adjustment.")

		if (box1 or box4) and not (self.get("adjustment_reason") or "").strip():
			frappe.throw("Enter the reason for the adjustment, including the "
				"return period and invoices it corrects. HMRC expects you to "
				"keep a record of each correction.")

	def check_error_correction_limit(self):

		net = abs(flt(self.get("vat_output_adjustment"), 2) -
				  flt(self.get("vat_input_adjustment"), 2))
		if net <= ERROR_CORRECTION_LIMIT:
			return

		box6 = flt(self.total_output_exvat)
		if net > ERROR_CORRECTION_CEILING or net > box6 * 0.01:
			frappe.throw("The net value of the adjustments (%s) is above the "
				"limit for correcting errors on the next return. Report the "
				"errors to HMRC separately instead (VAT Notice 700/45, "
				"section 4)." % frappe.format_value(net, {"fieldtype": "Currency"}))


def get_adjustments(doc):
	"""Box 1 and Box 4 corrections for errors on earlier returns."""
	return {
		"vatDueSales": flt(doc.get("vat_output_adjustment"), 2),
		"vatReclaimedCurrPeriod": flt(doc.get("vat_input_adjustment"), 2),
		"reason": doc.get("adjustment_reason")
	}


def calculate_vat_return(doc, drilldown=None):
	return get_vat_return(doc.company,
						  doc.period_start_date,
						  doc.period_end_date,
						  drilldown,
						  adjustments=get_adjustments(doc))


def warn_lines_without_template(period_start_date, period_end_date):
	"""Lines with no Item Tax Template are left out of the return. Say so,
	rather than dropping them silently."""

	missing = []
	for invoice_type in ("Sales", "Purchase"):
		missing += frappe.db.sql_list("""
			select distinct invoice.name from
				`tab{invoice_type} Invoice` invoice
			inner join `tab{invoice_type} Invoice Item` ii on
				ii.parent = invoice.name
			where
				invoice.docstatus = 1 and
				invoice.posting_date >= %s and
				invoice.posting_date <= %s and
				ifnull(ii.item_tax_template, '') = ''
			""".format(invoice_type=invoice_type),
			(period_start_date, period_end_date))

	if missing:
		frappe.msgprint(
			"These invoices have lines with no Item Tax Template, so those "
			"lines are not in this return: %s" % ", ".join(missing),
			title="Lines left out of the VAT return", indicator="orange")


def get_transactions(company, invoice_type, period_start_date, period_end_date):

	transactions = frappe.db.sql("""
		select invoice.name as name, ii.item_name, ii.item_code, ii.idx,
			ii.base_amount, ii.item_tax_template,
		tt.vat_rate, tt.vat_is_reverse_charge, tt.vat_transaction_type,
		tt.vat_rules from 
			`tab{invoice_type} Invoice` invoice
		inner join `tab{invoice_type} Invoice Item` ii on
			ii.parent = invoice.name
		left join `tabItem Tax Template` tt on
			tt.name = ii.item_tax_template
		where
			invoice.docstatus = 1 and
			invoice.posting_date >= %s and
			invoice.posting_date <= %s and
			tt.vat_is_in_vat_return = 1
		""".format(invoice_type=invoice_type), 
			(period_start_date, period_end_date), as_dict=True)

	# Calculate the VAT for each line
	for t in transactions:
		if t["item_tax_template"] is None:
			frappe.throw(
				"Item Tax Template not set on one of the lines in invoice %s" %
				t["name"])
		if t["vat_rate"] is None:
			frappe.throw("VAT rate not set on Item Tax Template %s" %
				t["item_tax_template"])

		# Round each line to the penny, as ERPNext does when it posts VAT, so
		# the return agrees with the ledger.
		t["vat"] = flt(t["base_amount"] * (t["vat_rate"] / 100.0), 2)

	return transactions


def get_vat_return(company, period_start_date, period_end_date, drilldown=None,
				   adjustments=None):

	if period_start_date > period_end_date:
		frappe.throw("Cannot create VAT return where start date is after the end date.")

	# VAT return as required by UK Government API (HMRC).
	vat_return = {k: 0.0 for k in vat_return_schema}
	vat_return["finalised"] = False

	# If requested, initialise drilldown
	if drilldown is not None:
		for k in vat_return_schema:
			drilldown[k] = []

	# We centralise all changes to the VAT return so we can record the source
	# of each change.
	def increment(item, field_list):
		for field in field_list:
			amount = item[vat_return_schema[field][1]]
			vat_return[field] += amount
			if drilldown is not None:
				drilldown[field] += [[item, amount]]

	#
	# SALES (Output VAT)
	#
	for sale in get_transactions(company, "Sales", 
								 period_start_date, period_end_date):
		
		# EU rules
		if sale["vat_rules"]=="EU":

			if sale["vat_transaction_type"]=="Goods":
				# Boxes 6 and 8
				increment(sale, 
					("totalValueSalesExVAT", "totalValueGoodsSuppliedExVAT"))
			elif sale["vat_transaction_type"]=="Services":
				# Box 6
				increment(sale, ("totalValueSalesExVAT",))
			else:
				frappe.throw(
					"Unsupported vat_transaction_type '%s' for EU rules" % 
					repr(sale["vat_transaction_type"]))

		# UK trade
		elif sale["vat_rules"]=="UK":

			# Boxes 1 and 6
			increment(sale, ("vatDueSales","totalValueSalesExVAT"))
		
		# RoW
		elif sale["vat_rules"]=="Rest of World":

			if sale["vat_transaction_type"]=="Goods" or \
			   sale["vat_transaction_type"]=="Services":
				# Box 6
				increment(sale, ("totalValueSalesExVAT",))
			else:
				frappe.throw(
					"Unsupported vat_transaction_type '%s' for RoW rules" % 
					repr(sale["vat_transaction_type"]))

		else:
			frappe.throw("Unknown vat_rules: %s" % repr(sale["vat_rules"]))

	#
	# Purchases (Input VAT)
	#
	for purchase in get_transactions(company, "Purchase", 
									 period_start_date, period_end_date):
		
		# EU rules
		if purchase["vat_rules"]=="EU":

			if purchase["vat_transaction_type"]=="Goods":

				# Boxes 2, 4, 7 and 9
				increment(purchase, 
					("vatDueAcquisitions", "vatReclaimedCurrPeriod",
					 "totalValuePurchasesExVAT","totalAcquisitionsExVAT"))

			elif purchase["vat_transaction_type"]=="Services":

				# Boxes 1, 4, 6 and 7
				increment(purchase,
					("vatDueSales", "vatReclaimedCurrPeriod", 
					 "totalValueSalesExVAT", "totalValuePurchasesExVAT"))

			else:
				frappe.throw(
					"Unsupported vat_transaction_type '%s' for EU rules" % 
					repr(purchase["vat_transaction_type"]))

		# UK trade
		elif purchase["vat_rules"]=="UK":

			if purchase["vat_is_reverse_charge"]:

				# Boxes 1, 4, 6 and 7
				increment(purchase,
					("vatDueSales", "vatReclaimedCurrPeriod", 
					 "totalValueSalesExVAT", "totalValuePurchasesExVAT"))

			else:

				# Boxes 4 and 7
				increment(purchase,
					("vatReclaimedCurrPeriod", "totalValuePurchasesExVAT"))
	
		# RoW
		elif purchase["vat_rules"]=="Rest of World":

			if purchase["vat_transaction_type"]=="Goods":

				# Boxes 4 and 7
				increment(purchase,
					("vatReclaimedCurrPeriod", "totalValuePurchasesExVAT"))

			elif purchase["vat_transaction_type"]=="Services":

				# Boxes 1, 4, 6 and 7
				increment(purchase,
					("vatDueSales", "vatReclaimedCurrPeriod", 
					 "totalValueSalesExVAT", "totalValuePurchasesExVAT"))

		
		else:
			frappe.throw("Unknown vat_rules: %s" % repr(purchase["vat_rules"]))

	# Round the boxes that are summed from invoices before deriving Boxes 3
	# and 5 from them. HMRC rejects a return unless Box 3 = Box 1 + Box 2 and
	# Box 5 = Box 3 - Box 4 exactly, using the submitted (rounded) figures.
	for f in ("vatDueSales", "vatDueAcquisitions", "vatReclaimedCurrPeriod",
			  "totalValueSalesExVAT", "totalValuePurchasesExVAT",
			  "totalValueGoodsSuppliedExVAT", "totalAcquisitionsExVAT"):
		vat_return[f] = flt(vat_return[f], vat_return_schema[f][3])

	# Corrections for errors on earlier returns (VAT Notice 700/45, Method 1)
	# are added to Box 1 (VAT owed to HMRC) or Box 4 (VAT owed to you).
	if adjustments:
		for f in ("vatDueSales", "vatReclaimedCurrPeriod"):
			amount = flt(adjustments.get(f), 2)
			if not amount:
				continue
			vat_return[f] = flt(vat_return[f] + amount, 2)
			if drilldown is not None:
				drilldown[f] += [[frappe._dict(
					name="Correction of earlier returns",
					item_name=adjustments.get("reason") or "",
					item_tax_template=None), amount]]

	# Box 3 = Box 1 + Box 2
	vat_return["totalVatDue"] = flt(vat_return["vatDueSales"] +
		vat_return["vatDueAcquisitions"], 2)

	# Box 5 = difference between Box 3 and Box 4. HMRC requires a positive
	# figure; whether it is payable or repayable follows from Boxes 3 and 4.
	vat_return["netVatDue"] = flt(abs(vat_return["totalVatDue"] -
		vat_return["vatReclaimedCurrPeriod"]), 2)

	return vat_return

@frappe.whitelist()
def get_open_obligations(company_name):	
	return vat_api.get_open_obligations(company_name)

def check_figures_are_current(doc):

	current = calculate_vat_return(doc)
	changed = []
	for box, fieldname in box_fields.items():
		precision = vat_return_schema[box][3]
		if flt(doc.get(fieldname), precision) != flt(current[box], precision):
			changed.append("Box %d: saved %s, now %s" % (
				vat_return_schema[box][0],
				flt(doc.get(fieldname), precision),
				flt(current[box], precision)))

	if changed:
		frappe.throw("The invoices for this period have changed since the "
			"return was saved, so it has not been sent to HMRC. Click "
			"'Save and recalculate', check the figures and submit again."
			"<br><br>" + "<br>".join(changed),
			title="VAT return is out of date")


@frappe.whitelist()
def submit_vat_return(name, is_finalised):

	doc = frappe.get_doc("UK VAT Return", name)

	if doc.docstatus != 0:
		frappe.throw("This VAT return has already been submitted or cancelled.")

	# The boxes are stored when the return is saved. If an invoice has been
	# submitted, cancelled or amended since then, the stored figures are out
	# of date. Refuse to send them rather than submit the wrong return.
	check_figures_are_current(doc)

	# Match dates on the form to an open obligation
	obligations = vat_api.get_open_obligations(doc.company)
	selected_obligation = None
	for o in obligations:
		start = datetime.datetime.strptime(o["start"], "%Y-%m-%d").date()
		end = datetime.datetime.strptime(o["end"], "%Y-%m-%d").date()
		if doc.period_start_date==start and doc.period_end_date==end:
			selected_obligation = o
			break
	else:
		frappe.throw("The selected dates do not match any open HMRC obligations")

	# Generate submission document
	vat_return = {
		"vatDueSales": doc.vat_output,
		"vatDueAcquisitions": doc.vat_eu_acquisitions,
		"totalVatDue": doc.vat_due_total,
		"vatReclaimedCurrPeriod": doc.vat_input,
		"netVatDue": doc.vat_net,
		"totalValueSalesExVAT": int(doc.total_output_exvat),
		"totalValuePurchasesExVAT": int(doc.total_input_exvat),
		"totalValueGoodsSuppliedExVAT": int(doc.total_ec_goods_output),
		"totalAcquisitionsExVAT": int(doc.total_ec_goods_input),
		"finalised": True if is_finalised else False,
		"periodKey": selected_obligation["periodKey"]
	}

	# Submit return to HMRC
	response, headers = vat_api.submit_return(doc.company, vat_return)
	if response.get("errors"):
		frappe.throw("\n".join({e["message"] for e in response["errors"]}),
		title=response["message"])

	# Save response
	doc.hmrc_correlation_id = headers["X-CorrelationId"]
	doc.hmrc_receipt_id = headers["Receipt-ID"]
	doc.hmrc_receipt_timestamp = headers["Receipt-Timestamp"]
	doc.hmrc_period_key = selected_obligation["periodKey"]
	doc.hmrc_processing_date = response.get("processingDate")
	doc.hmrc_form_bundle_number = response.get("formBundleNumber")
	doc.hmrc_payment_indicator = response.get("paymentIndicator")
	doc.hmrc_charge_reference_number = response.get("chargeRefNumber")
	doc.hmrc_vrn = vat_api.get_vrn(doc.company)
	doc.is_finalised = is_finalised
	doc.docstatus = 1
	doc.save()
