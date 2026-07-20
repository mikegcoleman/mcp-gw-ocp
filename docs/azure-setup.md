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

### 1b. Set the Application ID URI and add a delegated scope

1. In the app registration, go to **Expose an API**
2. Click **Set** next to Application ID URI
3. Accept the default `api://<client-id>` or use a readable name like `api://mcp-gateway`
4. Click **Save**

Record the Application ID URI — this is `ENTRA_RESOURCE_URI`.

Now add a **delegated scope** on the same page (required — clients acquire *delegated* user
tokens against this scope, and §1e's pre-authorization references it):

5. Click **Add a scope**
6. Scope name: `access`
7. Who can consent: **Admins and users**
8. Fill in the consent display names/descriptions (e.g. "Access the MCP Gateway")
9. State: **Enabled**, click **Add scope**

> **App role vs. scope:** §1c below adds an *app role* (`MCPGateway.User`, an authorization
> label in the `roles` claim). This scope (`access`) is what makes *delegated* token
> acquisition work at all. You need both.

### 1c. Add an app role

App roles appear as the `roles` claim in the JWT and can be used for authorization.

1. Go to **App roles → Create app role**
2. Display name: `MCP Gateway User`
3. Allowed member types: **Users/Groups**
4. Value: `MCPGateway.User`
5. Description: `Allows access to the MCP Gateway`
6. Enable the role, click **Apply**

### 1c-2. Add team-scoped app roles (Milestone 3)

The `MCPGateway.User` role above gates gateway entry. Team roles add **server-level segmentation**:
users in different Entra groups see different MCP servers, with no sidecar code changes required
(the JWT `roles` claim is already extracted and passed to the gateway policy engine).

Create two additional app roles — one per team:

**MCP Team A** (Opine access):
1. Go to **App roles → Create app role**
2. Display name: `MCP Team A`
3. Allowed member types: **Users/Groups**
4. Value: `mcp-team-a`
5. Description: `Opine AI access via MCP Gateway`
6. Enable the role, click **Apply**

**MCP Team B** (Granola access):
1. Go to **App roles → Create app role**
2. Display name: `MCP Team B`
3. Allowed member types: **Users/Groups**
4. Value: `mcp-team-b`
5. Description: `Granola meeting notes access via MCP Gateway`
6. Enable the role, click **Apply**

> **The `value` field is what appears in the JWT `roles` claim.** It must exactly match (case-sensitive)
> the `role:` field in `MCPGateway.spec.policies.rules`. The display name is only shown in the portal.

### 1d. Set token version to v2.0

The sidecar validates v2.0 tokens. You must set this explicitly.

1. Go to **Manifest** (in the app registration sidebar)
2. Find `"accessTokenAcceptedVersion"` and change its value from `null` to `2`
3. Click **Save**

> **Why this matters:** With v1.0 tokens the `aud` claim contains the Application ID URI
> (e.g. `api://mcp-gateway`). With v2.0 tokens it contains the client ID GUID. The sidecar
> sets `ENTRA_AUDIENCE` to the client ID GUID, so tokens must be v2.0.

### 1e. Pre-authorize your MCP client

Add your client's public client ID to the authorized client applications so users are not
prompted for admin consent.

**VS Code Copilot:**
1. Go to **Expose an API → Add a client application**
2. Client ID: `aebc6443-996d-45c2-90f0-388ff96faa56` (VS Code)
3. Authorized scopes: check **`access`** (created in §1b)
4. Click **Add application**

Also pre-authorize the **Azure CLI** client `04b07795-8ddb-461a-bbee-02f9e1bf7b46` (same steps,
`access` scope) — it's what `az account get-access-token` uses, and it powers the Step 8 test and
the Claude Code token helper. Without it, CLI token requests fail with `AADSTS65001` (no consent).

### 1e-2. Enable desktop (public-client) sign-in

Interactive MCP clients (VS Code Copilot, and any desktop client) complete OAuth via a **loopback
redirect** and can't hold a client secret, so the app must allow public-client flows:

1. Go to **Authentication → Add a platform → Mobile and desktop applications**
2. Add redirect URI **`http://localhost`** (no port — Entra then matches any runtime port)
3. Under **Advanced settings**, set **Allow public client flows = Yes**
4. Save

> Without this, interactive sign-in fails (Entra rejects the loopback redirect / requires a
> secret). This is separate from §1f's client secret, which the **sidecar** uses for Key Vault —
> not the interactive user flow.

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

### 1g-2. Create Entra security groups and add users (Milestone 3)

Create two security groups — one per team — then add the users to the appropriate groups.
Finally, assign each group to its app role so the role appears in member JWTs.

**Create the groups:**

1. Go to **Azure Portal → Microsoft Entra ID → Groups → New group**
2. Group type: **Security**
3. Group name: `mcp-team-a`
4. Add member: `msmikecol@hotmail.com` (alice — a B2B guest; must have accepted the invitation)
5. Click **Create**

Repeat for team-b:

1. **New group**, type: **Security**, name: `mcp-team-b`
2. Add member: `mike.coleman@docker.co` (bob — a B2B guest; must have accepted the invitation)
3. Click **Create**

> To add more users to a team later: **Groups → mcp-team-a → Members → Add members**.

**Assign each group to its app role:**

1. Go to **Azure Portal → Enterprise applications → mcp-gateway → Users and groups → Add user/group**
2. Select group: **mcp-team-a** → Role: **MCP Team A** → **Assign**
3. Repeat: group **mcp-team-b** → Role: **MCP Team B** → **Assign**

Both groups also need the base **MCP Gateway User** role from §1g so members can authenticate
to the gateway at all:

4. Add user/group → select **mcp-team-a** → Role: **MCP Gateway User** → **Assign**
5. Add user/group → select **mcp-team-b** → Role: **MCP Gateway User** → **Assign**

> When a group is assigned to an app role, **all group members** get that role in their JWT
> automatically. Adding or removing a user from the Entra group is the only change needed to
> grant or revoke their access — no gateway redeployment required.

### 1h. Register the Entra DCR Proxy application (Milestone 3)

The DCR proxy needs its own Entra app registration so it can exchange RFC 7591 Dynamic Client
Registration requests for real Entra OAuth flows. This is separate from the main `mcp-gateway`
registration — it acts as the broker between MCP clients (Claude Code, Claude Desktop) and Entra.

**Create the registration:**

1. Go to **Azure Portal → Microsoft Entra ID → App registrations → New registration**
2. Name: `mcp-gateway-dcr-proxy`
3. Supported account types: **Single tenant**
4. Redirect URI: **Web** → `https://mcp-gw-dp.<CLUSTER_DOMAIN>/dcr/callback`
5. Click **Register**

Record the **Application (client) ID** for the DCR proxy — this is `PROXY_ENTRA_CLIENT_ID`.

**Create a client secret:**

1. Go to **Certificates & secrets → Client secrets → New client secret**
2. Description: `dcr-proxy`, expiry: 1 year
3. Click **Add** and copy the secret value immediately — this is `PROXY_ENTRA_CLIENT_SECRET`

**Grant access to the gateway app scope:**

The DCR proxy needs permission to request tokens for the gateway app's `access` scope:

1. Go to **API permissions → Add a permission → My APIs**
2. Select the `mcp-gateway` app registration
3. Choose **Delegated permissions** → check **access**
4. Click **Add permissions**
5. Click **Grant admin consent for [tenant]** (required — the proxy acts on behalf of users)

**Pre-authorize the DCR proxy client for the gateway scope:**

1. Open the `mcp-gateway` app registration (the gateway app, not the proxy)
2. Go to **Expose an API → Add a client application**
3. Client ID: the DCR proxy's `PROXY_ENTRA_CLIENT_ID` from above
4. Authorized scopes: check **access**
5. Click **Add application**

**Create the K8s secret:**

```bash
GATEWAY_APPID=<mcp-gateway-app-client-id>     # from §1a
TENANT_ID=<directory-tenant-id>               # from §1a
DCR_PROXY_CLIENT_ID=<dcr-proxy-client-id>     # from this step
DCR_PROXY_CLIENT_SECRET=<dcr-proxy-secret>    # from this step

oc create secret generic entra-dcr-proxy-credentials \
  --from-literal=entra-tenant-id="$TENANT_ID" \
  --from-literal=dcr-proxy-client-id="$DCR_PROXY_CLIENT_ID" \
  --from-literal=dcr-proxy-client-secret="$DCR_PROXY_CLIENT_SECRET" \
  --from-literal=entra-app-client-id="$GATEWAY_APPID" \
  -n mcp-gateway
```

---

**Verify the roles are in the token** before deploying. With the Azure CLI client pre-authorized (§1e):

```bash
APPID=<your-application-client-id>

# Log in as alice and decode the roles claim
az login --allow-no-subscriptions --username msmikecol@hotmail.com
az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv \
  | cut -d. -f2 | base64 -d 2>/dev/null | jq '.roles'
# alice expects: ["MCPGateway.User", "mcp-team-a"]

# Log in as bob and decode the roles claim
az login --allow-no-subscriptions --username mike.coleman@docker.co
az account get-access-token --scope "api://$APPID/access" --query accessToken -o tsv \
  | cut -d. -f2 | base64 -d 2>/dev/null | jq '.roles'
# bob expects: ["MCPGateway.User", "mcp-team-b"]
```

> If `roles` is missing or empty, the user is not yet a member of a group that is assigned to
> the app role. Group-to-role assignments can take a minute to propagate.

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

---

## Appendix — CLI alternative (`az`)

The scripted equivalent of everything above. This is the exact sequence validated against a live
tenant, and it wires up the delegated scope + Azure CLI consent so the Step 8 token test works.
Requires `az login` with **Application Administrator** (Entra) and **Contributor** on the subscription.

```bash
# ---- inputs ----
APP_NAME=mcp-gateway
KV=mcp-gw-kv-$RANDOM                 # must be globally unique, 3-24 chars
RG=<your-resource-group>            # e.g. the ARO resource group
LOC=<your-region>                   # e.g. eastus
AZCLI=04b07795-8ddb-461a-bbee-02f9e1bf7b46   # Microsoft Azure CLI public client (for CLI token test)

TENANT=$(az account show --query tenantId -o tsv)

# ---- 1a-1c: app registration + identifier URI + app role ----
APPID=$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
OBJID=$(az ad app show --id "$APPID" --query id -o tsv)
az ad app update --id "$APPID" --identifier-uris "api://$APPID"
ROLE_ID=$(cat /proc/sys/kernel/random/uuid)
az ad app update --id "$APPID" --app-roles "[{\"allowedMemberTypes\":[\"User\"],\"displayName\":\"MCP Gateway User\",\"description\":\"Allows access to the MCP Gateway\",\"value\":\"MCPGateway.User\",\"id\":\"$ROLE_ID\",\"isEnabled\":true}]"

# ---- 1c-2: team-scoped app roles (Milestone 3) ----
TEAM_A_ROLE_ID=$(cat /proc/sys/kernel/random/uuid)
TEAM_B_ROLE_ID=$(cat /proc/sys/kernel/random/uuid)
# Fetch existing app-roles array, append the two new ones
EXISTING_ROLES=$(az ad app show --id "$APPID" --query appRoles -o json)
az ad app update --id "$APPID" --app-roles "$(echo "$EXISTING_ROLES" | python3 -c "
import json,sys
roles = json.load(sys.stdin)
roles += [
  {\"allowedMemberTypes\":[\"User\"],\"displayName\":\"MCP Team A\",\"description\":\"Opine AI access via MCP Gateway\",\"value\":\"mcp-team-a\",\"id\":\"$TEAM_A_ROLE_ID\",\"isEnabled\":True},
  {\"allowedMemberTypes\":[\"User\"],\"displayName\":\"MCP Team B\",\"description\":\"Granola meeting notes access via MCP Gateway\",\"value\":\"mcp-team-b\",\"id\":\"$TEAM_B_ROLE_ID\",\"isEnabled\":True}
]
print(json.dumps(roles))
")"

# ---- 1g-2: create security groups, add users, assign groups to app roles ----
ALICE_OID=$(az ad user show --id "msmikecol@hotmail.com" --query id -o tsv)
BOB_OID=$(az ad user show --id "mike.coleman@docker.co" --query id -o tsv)

# Create security groups
TEAM_A_GROUP=$(az ad group create --display-name "mcp-team-a" --mail-nickname "mcp-team-a" --query id -o tsv)
TEAM_B_GROUP=$(az ad group create --display-name "mcp-team-b" --mail-nickname "mcp-team-b" --query id -o tsv)

# Add users to their groups
az ad group member add --group "$TEAM_A_GROUP" --member-id "$ALICE_OID"
az ad group member add --group "$TEAM_B_GROUP" --member-id "$BOB_OID"

# Assign each group to its team app role
az rest --method POST --uri "https://graph.microsoft.com/v1.0/groups/$TEAM_A_GROUP/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$TEAM_A_GROUP\",\"resourceId\":\"$SP_OID\",\"appRoleId\":\"$TEAM_A_ROLE_ID\"}"
az rest --method POST --uri "https://graph.microsoft.com/v1.0/groups/$TEAM_B_GROUP/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$TEAM_B_GROUP\",\"resourceId\":\"$SP_OID\",\"appRoleId\":\"$TEAM_B_ROLE_ID\"}"

# Assign both groups to the base MCPGateway.User role (so members can authenticate at all)
az rest --method POST --uri "https://graph.microsoft.com/v1.0/groups/$TEAM_A_GROUP/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$TEAM_A_GROUP\",\"resourceId\":\"$SP_OID\",\"appRoleId\":\"$ROLE_ID\"}"
az rest --method POST --uri "https://graph.microsoft.com/v1.0/groups/$TEAM_B_GROUP/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$TEAM_B_GROUP\",\"resourceId\":\"$SP_OID\",\"appRoleId\":\"$ROLE_ID\"}"

# ---- 1b: delegated scope 'access' + 1d: v2 tokens ----
SCOPE_ID=$(cat /proc/sys/kernel/random/uuid)
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJID" \
  --headers "Content-Type=application/json" \
  --body "{\"api\":{\"requestedAccessTokenVersion\":2,\"oauth2PermissionScopes\":[{\"id\":\"$SCOPE_ID\",\"adminConsentDisplayName\":\"Access the MCP Gateway\",\"adminConsentDescription\":\"Access the MCP Gateway as the user\",\"value\":\"access\",\"type\":\"User\",\"isEnabled\":true}]}}"

# ---- 1e: pre-authorize VS Code + Azure CLI clients for the scope ----
VSCODE=aebc6443-996d-45c2-90f0-388ff96faa56
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJID" \
  --headers "Content-Type=application/json" \
  --body "{\"api\":{\"preAuthorizedApplications\":[{\"appId\":\"$VSCODE\",\"delegatedPermissionIds\":[\"$SCOPE_ID\"]},{\"appId\":\"$AZCLI\",\"delegatedPermissionIds\":[\"$SCOPE_ID\"]}]}}"

# ---- 1e-2: enable desktop (public-client) sign-in: loopback redirect + public flows ----
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJID" \
  --headers "Content-Type=application/json" \
  --body "{\"isFallbackPublicClient\": true, \"publicClient\": {\"redirectUris\": [\"http://localhost\"]}}"

# ---- 1f: client secret / 1g: service principal + self role assignment ----
SECRET=$(az ad app credential reset --id "$APPID" --display-name sidecar --years 1 --query password -o tsv)
SP_OID=$(az ad sp create --id "$APPID" --query id -o tsv)
MY_OID=$(az ad signed-in-user show --query id -o tsv)
az rest --method POST --uri "https://graph.microsoft.com/v1.0/users/$MY_OID/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$MY_OID\",\"resourceId\":\"$SP_OID\",\"appRoleId\":\"$ROLE_ID\"}"

# ---- consent so `az account get-access-token` works (Step 8 test) ----
AZCLI_SP=$(az ad sp show --id "$AZCLI" --query id -o tsv 2>/dev/null || az ad sp create --id "$AZCLI" --query id -o tsv)
az rest --method POST --uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants" \
  --headers "Content-Type=application/json" \
  --body "{\"clientId\":\"$AZCLI_SP\",\"consentType\":\"AllPrincipals\",\"resourceId\":\"$SP_OID\",\"scope\":\"access\"}"

# ---- 2: Key Vault (RBAC) + role grants ----
az keyvault create -n "$KV" -g "$RG" -l "$LOC" --enable-rbac-authorization true
KV_ID=$(az keyvault show -n "$KV" --query id -o tsv)
az role assignment create --assignee "$SP_OID" --role "Key Vault Secrets User"   --scope "$KV_ID"
az role assignment create --assignee "$MY_OID" --role "Key Vault Secrets Officer" --scope "$KV_ID"

# ---- 3: store a user's GitHub PAT (must be a VALID token with the scopes the tools need) ----
az keyvault secret set --vault-name "$KV" --name "github-pat-$MY_OID" --value "<the-users-github-PAT>"

# ---- values to record ----
echo "ENTRA_TENANT_ID / AZURE_TENANT_ID = $TENANT"
echo "ENTRA_CLIENT_ID / ENTRA_AUDIENCE / AZURE_CLIENT_ID = $APPID"
echo "ENTRA_RESOURCE_URI = api://$APPID"
echo "AZURE_KEYVAULT_URL = https://$KV.vault.azure.net/"
echo "AZURE_CLIENT_SECRET = $SECRET   # shown once"
```

> Key Vault RBAC role assignments take ~1-2 minutes to propagate before the secret-set / sidecar
> reads succeed. The `github-pat-<oid>` secret value must be a **valid** GitHub token with the
> scopes the GitHub MCP tools need — an expired/placeholder token yields `401 Bad credentials`
> from GitHub even though injection is working.

### 1h-cli: DCR Proxy app registration

```bash
# ---- inputs ----
# APPID must already be set (from the 1a block above, or: APPID=$(az ad app show --display-name mcp-gateway --query appId -o tsv))
# SCOPE_ID must already be set (the 'access' scope ID from the 1b block above)
CLUSTER_DOMAIN=<your-cluster-ingress-domain>   # e.g. apps.v9hpuzb1.eastus.aroapp.io

# ---- 1h: register the DCR proxy app ----
DCR_PROXY_APPID=$(az ad app create \
  --display-name "mcp-gateway-dcr-proxy" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "https://mcp-gw-dp.$CLUSTER_DOMAIN/dcr/callback" \
  --query appId -o tsv)
DCR_PROXY_OBJID=$(az ad app show --id "$DCR_PROXY_APPID" --query id -o tsv)

# Client secret
DCR_PROXY_SECRET=$(az ad app credential reset \
  --id "$DCR_PROXY_APPID" \
  --display-name dcr-proxy \
  --years 1 \
  --query password -o tsv)

# Create service principal for the proxy app
DCR_PROXY_SP=$(az ad sp create --id "$DCR_PROXY_APPID" --query id -o tsv)

# Grant the proxy permission to request tokens for the gateway app's 'access' scope
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$DCR_PROXY_OBJID" \
  --headers "Content-Type=application/json" \
  --body "{\"requiredResourceAccess\":[{\"resourceAppId\":\"$APPID\",\"resourceAccess\":[{\"id\":\"$SCOPE_ID\",\"type\":\"Scope\"}]}]}"

# Admin consent for the proxy → gateway permission (required for it to broker on behalf of users)
az rest --method POST --uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants" \
  --headers "Content-Type=application/json" \
  --body "{\"clientId\":\"$DCR_PROXY_SP\",\"consentType\":\"AllPrincipals\",\"resourceId\":\"$SP_OID\",\"scope\":\"access\"}"

# Pre-authorize the DCR proxy client on the gateway app's 'access' scope
# (so users aren't prompted for consent when the proxy requests tokens on their behalf)
GATEWAY_OBJID=$(az ad app show --id "$APPID" --query id -o tsv)
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$GATEWAY_OBJID" \
  --headers "Content-Type=application/json" \
  --body "{\"api\":{\"preAuthorizedApplications\":[{\"appId\":\"$DCR_PROXY_APPID\",\"delegatedPermissionIds\":[\"$SCOPE_ID\"]}]}}"

# Create the K8s secret
oc create secret generic entra-dcr-proxy-credentials \
  --from-literal=entra-tenant-id="$TENANT" \
  --from-literal=dcr-proxy-client-id="$DCR_PROXY_APPID" \
  --from-literal=dcr-proxy-client-secret="$DCR_PROXY_SECRET" \
  --from-literal=entra-app-client-id="$APPID" \
  -n mcp-gateway

echo "DCR_PROXY_APPID = $DCR_PROXY_APPID"
echo "DCR_PROXY_SECRET = $DCR_PROXY_SECRET   # shown once"
```
