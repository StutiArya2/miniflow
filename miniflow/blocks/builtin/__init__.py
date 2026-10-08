"""Built-in blocks; importing this package registers them.

==========  ===============================================================
Group       Blocks
==========  ===============================================================
data        http_request, read_csv, write_csv, read_json, write_json
transform   filter, select, aggregate, sort
systems     shell, sql
notify      email, log, wait
==========  ===============================================================
"""

from miniflow.blocks.builtin import data, notify, systems, transform

__all__ = ["data", "notify", "systems", "transform"]
