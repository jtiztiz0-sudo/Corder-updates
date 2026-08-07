"""
The shape of this app: the business it belongs to, and the tabs it has.

Auto-generated -- but this file is meant to be edited. It is the ONE place
that differs from every other app built this way.

Adding a field: put another dict in a module's "fields" list and restart. The
column is created on the next launch; existing records keep their data.

    {"name": "po_number", "label": "PO #", "type": "text", "html": "text",
     "required": False, "wide": False, "options": None, "step": None}

Field types: text, textarea, number, money, date, time, phone, email, select
(needs "options"), check.

Per-module extras:
    sort          ORDER BY clause for the list page
    date_field    which date drives "today" on the dashboard
    sum_field     which number gets totalled at the bottom of the list
    status_field  which select gets colour-coded
    alert         {"field": ..., "vs": ..., "label": ...} -> flags a row when
                  field <= vs (used by Inventory for low stock)
"""

BUSINESS = {'name': 'T-Town Closet',
 'id': 't_town_closet',
 'tagline': 'it needs to track once a rental is placed and IF it requires shipping. '
            'automatically make a shipping label  the cheapest option',
 'theme': {},
 'generated': '2026-08-06 22:47'}

MODULES = [{'key': 'rentals',
  'label': 'Rentals',
  'icon': '🔑',
  'table': 'rentals',
  'singular': 'rental',
  'fields': [{'name': 'item',
              'label': 'Item',
              'type': 'text',
              'html': 'text',
              'required': True,
              'wide': False,
              'options': None,
              'owner_only': False,
              'default': None,
              'step': None,
              'is_status': False},
             {'name': 'customer',
              'label': 'Customer',
              'type': 'text',
              'html': 'text',
              'required': False,
              'wide': False,
              'options': None,
              'owner_only': False,
              'default': None,
              'step': None,
              'is_status': False},
             {'name': 'out',
              'label': 'Out',
              'type': 'date',
              'html': 'date',
              'required': False,
              'wide': False,
              'options': None,
              'owner_only': False,
              'default': None,
              'step': None,
              'is_status': False},
             {'name': 'due_back',
              'label': 'Due back',
              'type': 'date',
              'html': 'date',
              'required': False,
              'wide': False,
              'options': None,
              'owner_only': False,
              'default': None,
              'step': None,
              'is_status': False},
             {'name': 'price',
              'label': 'Price',
              'type': 'money',
              'html': 'number',
              'required': False,
              'wide': False,
              'options': None,
              'owner_only': False,
              'default': None,
              'step': '0.01',
              'is_status': False},
             {'name': 'status',
              'label': 'Status',
              'type': 'select',
              'html': 'select',
              'required': False,
              'wide': False,
              'options': ['Reserved', 'Out', 'Returned', 'Late'],
              'owner_only': False,
              'default': None,
              'step': None,
              'is_status': True}],
  'sort': 'id DESC',
  'date_field': 'due_back',
  'sum_field': 'price',
  'status_field': 'status',
  'alert': None,
  'show_added': False,
  'archive': None},
 {'key': 'wishlist',
  'label': 'Wishlist',
  'icon': '💡',
  'table': 'wishlist',
  'singular': 'request',
  'fields': [{'name': 'request',
              'label': 'What would you like changed?',
              'type': 'textarea',
              'html': 'textarea',
              'required': True,
              'wide': True,
              'options': None,
              'owner_only': False,
              'default': None,
              'step': None,
              'is_status': False},
             {'name': 'status',
              'label': 'Status',
              'type': 'select',
              'html': 'select',
              'required': False,
              'wide': False,
              'options': ['New', 'Looking at it', 'Doing it', 'Done'],
              'owner_only': True,
              'default': 'New',
              'step': None,
              'is_status': True},
             {'name': 'fixed_at',
              'label': 'Fixed',
              'type': 'text',
              'html': 'text',
              'required': False,
              'wide': False,
              'options': None,
              'owner_only': True,
              'default': None,
              'step': None,
              'is_status': False}],
  'sort': 'id DESC',
  'date_field': None,
  'sum_field': None,
  'status_field': 'status',
  'alert': None,
  'show_added': True,
  'archive': {'done_value': 'Done',
              'stamp_field': 'fixed_at',
              'hours': 24,
              'done_label': 'Fixed',
              'open_label': 'Pending'}}]


def by_key(key):
    for m in MODULES:
        if m["key"] == key:
            return m
    return None
