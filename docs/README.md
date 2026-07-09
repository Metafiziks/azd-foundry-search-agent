# docs — Document Corpus

This directory contains the document corpus that the agent searches.

## Default content

The default docs are sample manufacturing operations documents:

- `safety/` — Safety procedures (e.g., lockout/tagout)
- `maintenance/` — Equipment maintenance manuals
- `quality/` — Quality control standards

## Replacing with your own documents

1. Delete the files in this directory (keep the directory structure or create your own).
2. Add your own `.txt`, `.pdf`, or `.docx` files in any subdirectory structure.
3. Update the agent instructions in `src/agent/main.py` to match your domain.
4. Run `azd up` to re-provision and re-deploy.

Supported formats: `.txt`, `.pdf`, `.docx`, `.md`, `.html`, `.json`, `.csv`

## Tips

- Organize documents in subdirectories by category — subdirectory names appear as metadata.
- Keep individual documents focused on a single topic for better retrieval quality.
- Plain text (`.txt`) documents give the most reliable extraction results.
