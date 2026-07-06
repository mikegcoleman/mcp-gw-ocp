# Azure Setup

This document covers everything you need to create in Azure before deploying the sidecar.
It assumes you have Contributor access on your Azure subscription and Application Administrator
in Entra ID.

At the end of this document there is a **Values to record** table. Fill it in as you go —
these values become environment variables on the sidecar pod.

---

## 1. Create an Entra ID App Registration

This registration serves two purposes: users authenticate against it (the sidecar validates
their JWTs), and the sidecar itself uses its credentials to access Key Vault.

### 1a. Register the application

1. Open **Azure Portal → Microsoft Entra ID → App registrations → New registration**
2. Set a name, e.g. `mcp-gateway`
3. Supported account types: **Single tenant** (accounts in this directory only)
4. Redirect URI: leave blank for now
5. Click **Register**

Record the **Application (client) ID** and **Directory (tenant) ID** from the overview page.

### 1b. Set the Application ID URI

1. In the app registration, go to **Expose an API**
2. Click **Set** next to Application ID URI
3. Accept the default `api://<client-id>` or use a readable name like `api://mcp-gateway`
4. Click **Save**

Record the Application ID URI — this is `ENTRA_RESOURCE_URI`.

### 1c. Add an app role

App roles appear as the `roles` claim in the JWT and can be used for authorization.

1. Go to **App roles → Create app role**
2. Display name: `MCP Gateway User`
3. Allowed member types: **Users/Groups**
4. Value: `MCPGateway.User`
5. Description: `Allows access to the MCP Gateway`
6. Enable the role, click **Apply**

### 1d. Set token version to v2.0

The sidecar validates v2.0 tokens. You must set this explicitly.

1. Go to **Manifest** (in the app registration sidebar)
2. Find `"accessTokenAcceptedVersion"` and change its value from `null` to `2`
3. Click **Save**

> **Why this matters:** With v1.0 tokens the `aud` claim contains the Application ID URI
> (e.g. `api://mcp-gateway`). With v2.0 tokens it contains the client ID GUID. The sidecar
> sets `ENTRA_AUDIENCE` to the client ID GUID, so tokens must be v2.0.

### 1e. Pre-authorize your MCP client

If you are using **VS Code Copilot**, add its public client ID to the authorized client
applications list so users are not prompted for admin consent:

1. Go to **Expose an API → Add a client application**
2. Client ID: `aebc6443-996d-45c2-90f0-388ff96faa56` (VS Code)
3. Authorized scopes: check the scope you created
4. Click **Add application**

For other clients, obtain their client ID and add them here in the same way.

### 1f. Create a client secret

The sidecar uses this secret to authenticate to Azure (for Key Vault access).

1. Go to **Certificates & secrets → Client secrets → New client secret**
2. Description: `mcp-gateway-sidecar`
3. Expiry: choose an appropriate duration (recommend 1 year for POC)
4. Click **Add**

**Copy the secret Value immediately** — it is only shown once. This is `AZURE_CLIENT_SECRET`.

### 1g. Assign users to the app role

Users must have the app role assigned to get it in their JWT `roles` claim.

1. Go to **Azure Portal → Enterprise applications → mcp-gateway** (the enterprise app object)
2. **Users and groups → Add user/group**
3. Select the users or groups who should have gateway access
4. Assign the **MCP Gateway User** role

---

## 2. Create an Azure Key Vault

1. Open **Azure Portal → Key vaults → Create**
2. **Resource group**: use an existing one or create new
3. **Key vault name**: choose a globally unique name, e.g. `mcp-gateway-kv`
   (record this — the vault URL is `https://<name>.vault.azure.net/`)
4. **Region**: any region accessible from your OpenShift cluster
5. **Pricing tier**: Standard
6. **Permission model**: **Azure role-based access control** (NOT Vault access policy)
7. Complete creation

### 2a. Grant the sidecar access to Key Vault

1. Open the Key Vault → **Access control (IAM) → Add role assignment**
2. Role: **Key Vault Secrets User**
3. Assign access to: **User, group, or service principal**
4. Members: search for your app registration name (`mcp-gateway`) and select it
5. Click **Review + assign**

> This grants the sidecar's service principal read access to secrets. It does **not**
> grant write access — PAT secrets are loaded manually (or by your ops tooling).

---

## 3. Load PAT secrets into Key Vault

For each user who needs GitHub access, create a secret in Key Vault.

### Secret naming

```
github-pat-{entra-object-id}
```

The Entra object ID (OID) is the user's immutable identifier in your tenant.

**Example:** User with OID `550e8400-e29b-41d4-a716-446655440000` gets secret:
```
Secret name:  github-pat-550e8400-e29b-41d4-a716-446655440000
Secret value: ghp_xxxxxxxxxxxxxxxxxxxx   (the GitHub PAT)
```

### Finding a user's OID

- **Azure Portal:** Entra ID → Users → select the user → the Object ID is on the overview page
- **Azure CLI:** `az ad user show --id user@example.com --query id -o tsv`

### GitHub PAT requirements

The PAT needs whatever GitHub API permissions the user's MCP tools require.
For general GitHub MCP usage (repos, issues, pull requests):

- **Fine-grained PAT:** select the repositories and permissions needed
- **Classic PAT:** at minimum `repo` scope

Create the PAT in GitHub → Settings → Developer settings → Personal access tokens,
then create the Key Vault secret:

```bash
az keyvault secret set \
  --vault-name mcp-gateway-kv \
  --name "github-pat-550e8400-e29b-41d4-a716-446655440000" \
  --value "ghp_xxxxxxxxxxxxxxxxxxxx"
```

Repeat for each user.

---

## 4. Values to record

Collect these before moving to OpenShift deployment.

| Value | Where to find it | Environment variable |
|-------|-----------------|----------------------|
| Directory (tenant) ID | App registration overview page | `ENTRA_TENANT_ID` and `AZURE_TENANT_ID` |
| Application (client) ID | App registration overview page | `ENTRA_CLIENT_ID`, `ENTRA_AUDIENCE`, and `AZURE_CLIENT_ID` |
| Client secret value | Certificates & secrets (shown once at creation) | `AZURE_CLIENT_SECRET` |
| Application ID URI | Expose an API page | `ENTRA_RESOURCE_URI` |
| Key Vault URL | Key Vault overview page (Vault URI field) | `AZURE_KEYVAULT_URL` |
| Public gateway URL | Your DNS / ingress hostname for the MCP Gateway | `GATEWAY_RESOURCE` |

> Note that **Tenant ID** and **client ID** each appear as two environment variables —
> once for the Entra JWT validation path and once for the Azure SDK credential path.
> They carry the same values.
