# 06 — Library and cache

**Library** stores reusable source text on this computer so later runs can reuse eligible material. It is separate from History, which records analyses and reports.

## Add papers to Library

Choose **Import papers**, then choose files or a folder. The available file types follow Callimachus’s enabled document readers; the file picker lists the current types. Callimachus checks local document details and presents the items under **Review pending imports**. Green rows are ready to save; amber rows need an identity check. Select a pending row, choose **Edit identity…**, correct its details, then use **Apply and verify**. Or retrieve metadata using a DOI, PMID, PMCID, arXiv ID, or ISBN and check that the returned details match the document.

Only verified rows can be selected for **Save selected**. Retrieving metadata alone does not save a document. Saving checks the reviewed file and records its text in Library; the file you originally selected remains yours.

## Back up and restore Library

Choose **Export library…** to save a copy or move your library to another computer. The ZIP contains archived original documents, active reusable texts, and their metadata. Keep this backup in a protected folder: documents in the archive are not encrypted.

Choose **Import library…** and select a copy exported by Callimachus. Import validates the entire archive before adding its contents, keeps existing entries, and skips duplicates. You can import the same backup again. Restored contents are recorded as user-supplied, so use your own backup or an archive you trust. Reports, credentials, settings, and imports still awaiting review remain separate.

During export and import, individual missing, incomplete, damaged, or unreadable files are skipped while other valid entries are processed. A final warning reports the number of affected items and provides names and reasons in its details. An unreadable ZIP container or library index prevents import. Importing a backup can also restore texts previously removed from the cache.

## Manage reusable entries

Use the search field and text-availability filter to find entries. **Delete selected** removes chosen entries from the reusable cache, and **Clear cache** removes the reusable entries. These actions preserve user originals and evidence already recorded in a run. Removing a cache entry affects future reuse; it does not rewrite an existing report.

A cached abstract does not prevent Callimachus from trying for full text in a later run when full text is possible. Check the evidence scope shown in each report rather than assuming every cached item is full text.

## Load and search large libraries

Library entries load in batches. Scroll to the bottom or choose **Show more** to request the next batch. Search and the text-availability filter apply across the stored entries, so you can search beyond the rows already displayed. A new search starts a fresh result list.
