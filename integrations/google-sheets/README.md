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
   Job Title, LinkedIn URL and Context are optional. A company domain and an email
   address are neither needed nor accepted.
2. Pick a campaign. Check the mapping the add-on guessed.
3. Select the rows and press **Process selected rows**. With nothing selected it
   offers every data row.
4. Come back later and press **Refresh results**. The spreadsheet does not need to
   stay open.

Rows read **Pending → Processing → Ready**, or **Could not prepare** with the
reason in `VMR Note`. Ready means a verified address *and* seven messages.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| "The Google Sheets integration is switched off" | `FEATURES__GOOGLE_SHEETS_INTEGRATION` is false, or the deployment was not restarted. |
| "VMR Outbound did not recognise this Google account" | The `aud` is not in `SHEETS__ALLOWED_AUDIENCES`, or the Google account has no active VMR Outbound account, or that account is disabled. All three answer identically on purpose. |
| "Google did not provide an identity token" | The script is not attached to a standard Cloud project (step 2), or the `openid` scope was not granted — remove the add-on's authorisation and re-authorise. |
| Rows stay Pending forever | `scripts/run_agent_worker.py` is not running. |
| "The company could not be identified from this name" | Use the company's exact registered name, or capture the person through the VMR extension where a domain can be confirmed by hand. |
| Every row says "Could not prepare" with a campaign message | The campaign has an Agent switched off that the pipeline needs. Fix it in the app; nothing in the sheet can. |

## Tests

```
cd integrations/google-sheets
node --test
```

No dependencies, no network, no Google account. The tests build a fake grid and
prove the thing that actually matters: a result can never land on the wrong row —
under sorting, insertion, deletion, a deleted key column, a partly refreshed
sheet and a retried submission.
