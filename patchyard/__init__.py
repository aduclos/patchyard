"""Patchyard: nobody should spend cumulative hours worrying about patching and
updates across their network.

Machines and network devices keep themselves updated; Patchyard watches them, tells
you only what needs a person, and leaves reboots to you. Generic engine: everything
site-specific lives in the site configuration (patchyard/config.py) and the Ansible
inventory (patchyard/policy.py).
"""
__version__ = "0.2.0.dev0"
