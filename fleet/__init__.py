"""Fleet patching: keep a fleet of machines updated, with reboots left to people.

Generic engine; everything site-specific lives in the site configuration
(fleet/config.py) and the Ansible inventory (fleet/policy.py).
"""
__version__ = "0.2.0.dev0"
