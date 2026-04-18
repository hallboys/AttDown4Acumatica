# Exposing `ComplianceDocument` on the Default endpoint

The stock Acumatica Default endpoint (through 25R2) does **not** expose the
`ComplianceDocument` entity. Extend it once so this tool can query and download
attachments.

## Steps (Acumatica UI)

1. Open **Web Service Endpoints (SM207060)**.
2. Select endpoint **Default** → choose the latest version (e.g. `24.200.001`).
3. Click **Extend Endpoint**. Acumatica creates an editable copy, e.g.
   `Default/24.200.001+1`.
4. In the extension, click **+** to add a top-level entity:
   - **Object name:** `ComplianceDocument`
   - **Mapped object:** `ComplianceDocumentEntry` (graph)
5. Map the fields you need. Minimum recommended set:
   - `RefNbr`, `Type`, `Status`, `Date`
   - `Vendor`, `Customer`, `Project`, `CostTask`
   - `ExpirationDate`, `ReceivedDate`
   - `CreatedDateTime`, `LastModifiedDateTime`
6. Add a sub-entity called **`Files`** mapped to the system **Files** entity.
   This is what exposes attachment metadata (`id`, `filename`, `href`) for
   download.
7. Save and publish the extension.

## Verify

Configure this tool to use `Default/24.200.001+1`, then:

```bash
attdown entities --config config.yaml
```

`ComplianceDocument` should appear in the list of entities with attachments.

Or hit the endpoint directly:

```
GET {base}/entity/Default/24.200.001+1/ComplianceDocument
    ?$top=1&$expand=Files
```

The response should include a `Files: [...]` array on each record.

## Notes

- Acumatica versions evolve the Compliance module fields. If a field name
  doesn't map, check **CRCompliance** DAC in the Customization Project
  browser for the current name.
- Granting a Connected Application access: give the OAuth proxy user at
  minimum read access to the Compliance Management (CL301000) screen.
