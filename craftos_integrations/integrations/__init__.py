"""Integrations subpackage. Every immediate-child folder (or .py module)
that doesn't start with ``_`` is autoloaded at startup.

To add a new integration, create a folder ``<name>/`` containing:
  - ``__init__.py`` with:
      - a credential dataclass
      - an IntegrationSpec
      - an IntegrationHandler subclass decorated with @register_handler
      - a BasePlatformClient subclass decorated with @register_client
  - (optional) ``INTEGRATION.md`` documenting the integration
  - (optional) underscore-prefixed sibling helpers (e.g. ``_bridge_client.py``)

Helpers shared across multiple integrations live at this level with an
underscore prefix (``_google_common.py``, ``_lark_common.py``) — the
autoloader skips them.

See github/ for the canonical shape.
"""
