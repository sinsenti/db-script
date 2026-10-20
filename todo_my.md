* Save filter queries and raw SQL commands to `~/.cache/db_history` using `prompt_toolkit.history.FileHistory` i want to be able to pick sql query via nvim too. and also i want store all quotes and be able to open just file with sql commands(even when i write just thorugh filter(not exact) sql , it should work correctly)


**Column Visibility Toggling (`h`)**
* Open an interactive column toggle menu to temporarily hide wide or noisy columns (e.g. large JSON/text fields) to keep the primary ASCII table compact.
**System Clipboard Integration (`y` / `yy`)**
* Copy the currently focused cell (`y`) or entire selected row as JSON (`yy`) directly to the system clipboard via  `wl-copy`(default one),`xclip`, or `pbcopy`.
**Index & Constraint Inspector (`I`)**
* Expand schema discovery beyond column types to show primary keys, unique constraints, indexes, and active database triggers.
