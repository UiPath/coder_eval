# Evalboard Deployment (Azure App Service)

Production deployment of the Next.js evalboard at `flow-evalboard`. Operational reference only — history / decisions / dead ends are in git log.

## TL;DR

| | |
|---|---|
| **App URL** | `https://flow-evalboard.uipath-dev.com` (default `*.azurewebsites.net` hostname still works as fallback) |
| **Subscription** | `DevTest-ML-EA` (`5db48574-8a20-418f-b488-1fafd8d021df`) |
| **Resource group** | `rg-coder-eval-tests` (West US 2) |
| **Runtime** | Node 22 LTS on Linux, B1 SKU (~$9/mo) |
| **Auth** | Disabled — app serves anonymously (see below) |
| **Deploy model** | Run From Package (zip mounted from blob at container start) |

## Deploy

```bash
./scripts/deploy.sh
```

Script is authoritative — it builds, swaps pnpm's symlinks for a flat npm `node_modules`, smoke-tests locally, uploads the zip to blob storage, and restarts the app. See `scripts/deploy.sh` for details.

## Day-2 Operations

- **Tail logs**: `az webapp log tail --name flow-evalboard --resource-group rg-coder-eval-tests`
- **SSH into container**: `az webapp ssh --name flow-evalboard --resource-group rg-coder-eval-tests`
- **Change env vars**: `az webapp config appsettings set --name flow-evalboard --resource-group rg-coder-eval-tests --settings KEY=VALUE` (restarts the app)
- **Roll back**: re-upload a previous zip to the same blob name (`deploys/flow-evalboard.zip`) and restart
- **Scale up**: `az appservice plan update --name ASP-rgcoderevaltests-845a --resource-group rg-coder-eval-tests --sku P0V3`
- **Scale out**: `az appservice plan update --name ASP-rgcoderevaltests-845a --resource-group rg-coder-eval-tests --number-of-workers 2`

## Storage Access

`lib/blob.ts` uses `DefaultAzureCredential`. On App Service it picks up the system-assigned MI (principal `617f3082-1dc6-4900-b025-76169eaae0ec`), which has **Storage Blob Data Reader** on the `coderevaltests` storage account. No code changes needed.

To re-grant after accidental deletion (requires PIM-activated `User Access Administrator - UiPathCustomRole` on `DevTest-ML-EA-mgmt`):

```bash
az role assignment create \
  --assignee-object-id 617f3082-1dc6-4900-b025-76169eaae0ec \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope "/subscriptions/5db48574-8a20-418f-b488-1fafd8d021df/resourceGroups/rg-coder-eval-tests/providers/Microsoft.Storage/storageAccounts/coderevaltests"
```

## Auth

Easy Auth is **disabled**. The app serves anonymously.

```
platform.enabled              = false
globalValidation.requireAuth  = false
globalValidation.unauthAction = AllowAnonymous
```

UiPath's tenant requires admin consent for all delegated scopes, including basic OIDC. Evalboard is internal tooling with a random hostname and no customer data, so we opted to go public instead of waiting on IT. Keep the URL in internal channels only.

The Entra app registration (`flow-evalboard`, client ID `d96ec2c5-d6fb-4674-8392-771ef01729c3`) and related app settings (`MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`, `WEBSITE_AUTH_AAD_ALLOWED_TENANTS`) are parked in place. To re-enable auth later, get admin consent via the open IT ticket: [IT-184799](https://uipath.atlassian.net/servicedesk/customer/portal/3/IT-184799).

## Open tickets

- [**IT-184799**](https://uipath.atlassian.net/servicedesk/customer/portal/3/IT-184799) — Entra admin consent for `openid profile email`. Required to re-enable Easy Auth (see Auth section).

## Reference IDs

| | |
|---|---|
| Subscription ID | `5db48574-8a20-418f-b488-1fafd8d021df` |
| Tenant ID (UiPath) | `d8353d2a-b153-4d17-8827-902c51f72357` |
| Web App MI Principal ID | `617f3082-1dc6-4900-b025-76169eaae0ec` |
| Entra app Client ID | `d96ec2c5-d6fb-4674-8392-771ef01729c3` |
| Storage account | `coderevaltests` / container `runs` |
| Owner | `bai.li@uipath.com` |
| Storage owner | `tomasz.religa@uipath.com` |
