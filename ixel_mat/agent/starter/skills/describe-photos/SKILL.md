---
name: describe-photos
description: Describes pictures in a folder, sorted and captioned, without modifying files.
---

1. Use list_dir to find entries whose kind is picture.
2. For each picture, use view_file to inspect it (at most 8 files).
3. Output the name and a short plain description of each picture.

This skill only reads. It never renames or moves a file unless you ask, and then it proposes each change for your approval.
