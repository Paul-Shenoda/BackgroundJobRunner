"""Handle allocation and recycling (lane-N, numbered per lane per user).

Uniqueness is enforced by the database's partial unique index, not by
application-level check-then-insert logic here.
"""
