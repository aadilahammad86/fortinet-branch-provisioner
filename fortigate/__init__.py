"""Shared FortiGate branch-provisioning engine.

The CLI scripts in scripts/ and the GUI (branch_gui.py) both drive this
package, so provisioning logic exists in exactly one place. Fix a bug here
and both front ends get it.

    client.py  -- login / CSRF / REST plumbing, config backup
    branch.py  -- interfaces, DHCP, policies, wan1, LAN, verification
    utm.py     -- web filter + application control
"""

from .client import FortiGate, FortiGateError, LoginError, load_env

__all__ = ["FortiGate", "FortiGateError", "LoginError", "load_env"]
