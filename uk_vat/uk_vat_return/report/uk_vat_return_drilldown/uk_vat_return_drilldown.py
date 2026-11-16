# -*- coding: utf-8 -*-
# Copyright (c) 2020 Software to Hardware Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from uk_vat.uk_vat_return.doctype.uk_vat_return.uk_vat_return import get_vat_return, get_adjustments, vat_return_schema

def execute(filters=None):

	if filters.company is None or \
		filters.period_start_date is None or \
		filters.period_end_date is None:

		return [], []

	columns = [	{'fieldname': 'box', 
				'label': 'VAT Return Field', 
				'fieldtype': 'Data', 
				'options': '', 
				'width': 300},
				{'fieldname': 'line_item', 
				 'label': 'Line Item',
				 'fieldtype': 'Data', 
				 'options': '', 
				 'width': 150
				 },
				{'fieldname': 'item_tax_template', 
				 'label': 'Item Tax Template',
				 'fieldtype': 'Link', 
				 'options': 'Item Tax Template', 
				 'width': 120
				 },
				{'fieldname': 'contribution',
				 'label': 'Contribution',
				 'fieldtype': 'Currency',
				 'options': '',
				 'width': 120}
	]

	# Include any corrections entered on the VAT return for this period, so
	# the drilldown totals agree with the return.
	adjustments = None
	vat_return_name = frappe.db.get_value("UK VAT Return",
		{"company": filters.company,
		 "period_start_date": filters.period_start_date,
		 "period_end_date": filters.period_end_date,
		 "docstatus": ["<", 2]},
		"name", order_by="modified desc")
	if vat_return_name:
		adjustments = get_adjustments(
			frappe.get_doc("UK VAT Return", vat_return_name))

	drilldown = {}
	vat_return = get_vat_return(filters.company,
								filters.period_start_date,
								filters.period_end_date,
								drilldown,
								adjustments=adjustments)

	box_order = [
		"vatDueSales",
		"vatDueAcquisitions",
		"totalVatDue",
		"vatReclaimedCurrPeriod",
		"netVatDue",
		"totalValueSalesExVAT",
		"totalValuePurchasesExVAT",
		"totalValueGoodsSuppliedExVAT",
		"totalAcquisitionsExVAT"	
	]

	data = []
	for box in box_order:
		box_name = "Box %d: %s" % (vat_return_schema[box][0], 
								   vat_return_schema[box][2])
		data += [
			{'box': box_name,
			 'contribution': vat_return[box],
                         'is_group': 1}
		]
		for drilldown_item in drilldown[box]:
			data += [
				{'box': drilldown_item[0].name,
				 'parent_box': box_name,
				 'line_item': drilldown_item[0].item_name,
				 'item_tax_template': drilldown_item[0].item_tax_template,
				 'contribution': drilldown_item[1],
				 'indent': 1}
			]

	return columns, data




 
