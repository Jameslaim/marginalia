"""Identity, ledger, and file layout — the parts that own state.

Everything here answers *what do we already have?*. `ids` canonicalises a video
ID, `ledger` records every ID ever seen and its verdict, and `library` builds
every path under the library root. No module outside this package constructs a
library path or opens the ledger database.
"""
