# VMR Outbound — Google Sheets add-on

A thin client. It detects columns, mints row keys, posts rows to VMR Outbound and
writes back a verified address and seven messages. It contains no research, no
verification, no personalization and no sending, and it never will —
`docs/GOOGLE_SHEETS_INTEGRATION.md` is the canonical description of why.

```
src/Contract.js   pure grid functions — header detection, row keys, result mapping
src/Sheet.js      the thin SpreadsheetApp layer
src/Api.js        the three HTTP calls and the credential
src/Menu.js       the menu, the sidebar's two actions
src/Sidebar.html  the sidebar
test/             Node tests over the pure functions and a fake sheet
```

Apps Script has no module system: every file shares one global scope, and clasp
converts `.js` to `.gs` on push. The files are `.js` so that `node --test` can run
the same source that ships.

## Install for the first user

You need: the VMR Outbound deployment reachable over HTTPS, an active VMR
Outbound account for the person who will use the sheet, and
`scripts/run_agent_worker.py` running.

**1. Turn the surface on.** In `/etc/vmr/vmr.env`:

```
FEATURES__GOOGLE_SHEETS_INTEGRATION=true
```

Restart. Every route still answers `401` until step 4 — that is correct.

**2. Create the Apps Script project.**

In the spreadsheet: **Extensions → Apps Script**. Then
**Project Settings → Google Cloud Platform (GCP) Project → Change project**, and
enter the deployment's existing Cloud project number. This step is required:
`ScriptApp.getIdentityToken()` returns nothing on a script using the default
project.

Push the source with [clasp](https://github.com/google/clasp):

```
npm install -g @google/clasp
clasp login
cd integrations/google-sheets
clasp clone <SCRIPT_ID>          # or `clasp create --type sheets`
clasp push
```

Or paste the files in by hand: four script files and one HTML file named
`Sidebar`, plus the `appsscript.json` manifest (enable "Show appsscript.json" in
Project Settings first).

**3. Open the sidebar.** Reload the spreadsheet, then
**Extensions → VMR Outbound → Open VMR Outbound**. Authorise when Google asks;
the consent screen should list the open spreadsheet, external requests and your
email address, and **nothing about Gmail or Drive**. If it asks for more, stop
and check the manifest.

Enter the deployment address (`https://…`) and press Save.

**4. Tell the deployment which add-on to trust.** The sidebar now shows a client
id under the address field. Copy it into `/etc/vmr/vmr.env`:

```
SHEETS__ALLOWED_AUDIENCES=["1234567890-abcdef.apps.googleusercontent.com"]
```

Restart. This is the check that stops a Google token minted for somebody else's
application being replayed against your deployment, so the value must be copied
from the sidebar rather than guessed from the Cloud console — the console lists
several client ids and only one of them will arrive.

**5. Reload the sidebar.** It should show `Connected as <your email>` and your
campaigns.

## Using it

1. Put **First Name**, **Last Name** and **Company Name** columns in the sheet.
   Job Title, LinkedIn URL, Context, **Email Address** and **Company Website** are
   optional. Supplying the last two is optional in the strongest sense: leave them
   out and the product discovers the address, verifies it and works out the
   company domain itself, exactly as before. Fill them in and it uses what you
   already know instead of going to find it —
   `docs/GOOGLE_SHEETS_INTEGRATION.md` §7a says precisely what that skips and what
   it does not. `Company Website` takes a URL or a bare domain.
2. Pick a campaign. Check the mapping the add-on guessed.
3. Select the rows and press **Process selected rows**. With nothing selected it
   offers every data row.
4. Come back later and press **Refresh results**. The spreadsheet does not need to
   stay open.

Rows read **Pending → Processing → Ready**, or **Could not prepare** with the
reason in `VMR Note`. Ready means a usable address *and* seven messages — either
an address the product verified, or one you supplied and it therefore never tried
to verify. Ready is not a deliverability claim, and the app still reports a
supplied mailbox as unchecked.

The add-on writes only `VMR `-prefixed columns — `VMR Status`,
`VMR Email Address`, `VMR Email 1`…`VMR Email 7`, `VMR Note`,
`VMR Last Updated`, `VMR Contact ID`, `VMR Campaign Contact ID`, and the hidden
`VMR Row Key`. **It never writes into a column your data is in**, and that is
enforced by refusing to reuse any column the header mapping claims, not by hoping
the names do not clash.

A sheet an earlier version of the add-on wrote to still has unprefixed
`Email Address` and `Email 1`…`Email 7` columns. They are left exactly as they
are — never written, never renamed — and the new `VMR ` columns appear beside
them on the next submit or refresh. Delete the old ones whenever you like.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| "The Google Sheets integration is switched off" | `FEATURES__GOOGLE_SHEETS_INTEGRATION` is false, or the deployment was not restarted. |
| "VMR Outbound did not recognise this Google account" | The `aud` is not in `SHEETS__ALLOWED_AUDIENCES`, or the Google account has no active VMR Outbound account, or that account is disabled. All three answer identically on purpose. |
| "Google did not provide an identity token" | The script is not attached to a standard Cloud project (step 2), or the `openid` scope was not granted — remove the add-on's authorisation and re-authorise. |
| Rows stay Pending forever | `scripts/run_agent_worker.py` is not running. |
| A row stays Pending and the app shows the Company stage blocked | The company has no established domain yet. Give it one — an import carrying `company_domain`, or a capture of someone there — and the stage resumes. The row is already a permanent Contact; nothing needs resubmitting. |
| Every row says "Could not prepare" with a campaign message | The campaign has an Agent switched off that the pipeline needs. Fix it in the app; nothing in the sheet can. |

## Tests

```
cd integrations/google-sheets
node --test
```

No dependencies, no network, no Google account. The tests build a fake grid and
prove the two things that actually matter: a result can never land on the wrong
row — under sorting, insertion, deletion, a deleted key column, a partly
refreshed sheet and a retried submission — and a result can never land on a
column the operator owns, including a sheet whose own columns are called
`Email Address`, `Company Website` and `Email 1`.
