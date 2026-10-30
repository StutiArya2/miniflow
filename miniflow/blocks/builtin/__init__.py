"""Built-in blocks; importing this package registers them.

==========  ===============================================================
Group       Blocks
==========  ===============================================================
data        http_request, read_csv, write_csv, read_json, write_json
transform   filter, select, aggregate, sort
systems     shell, sql
notify      email, log, wait
web         webhook, slack_message, teams_message, llm_prompt, s3_get, s3_put
llm         llm_map (a model call per row; offline simulated provider; tolerant reuse)
office      read_xlsx, write_xlsx
text        date_format, date_add, date_diff, text_template
databases   postgres, mysql (need the psycopg / pymysql packages)
==========  ===============================================================
"""

from miniflow.blocks.builtin import (
    data,
    databases,
    llm,
    notify,
    office,
    systems,
    text,
    transform,
    web,
)

__all__ = ["data", "databases", "llm", "notify", "office", "systems", "text", "transform", "web"]
