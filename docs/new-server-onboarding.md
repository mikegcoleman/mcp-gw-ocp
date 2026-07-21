# Onboarding a New MCP Server

Decision flow for adding a server under the group-based access model.
**IT** owns the MCPGateway CR, MCPServer CRs, and Entra setup. **Teams** own their catalog and policy ConfigMaps.

```mermaid
flowchart TD
    A([New MCP server request]) --> B{New team or\nexisting team?}

    B -->|New team| C1[IT: Create Entra App Role mcp-team-X\nin Azure app registration]
    C1 --> C2[IT: Create Entra Security Group\nAdd users to group\nAssign group to app role]
    C2 --> C3[IT: Create catalog-team-X.yaml ConfigMap\nUpdate mcpenvironment.yaml to reference it]
    C3 --> C4[IT: Apply pipeline RBAC\nkubectl apply -f manifests/rbac-pipeline.yaml\nMint team-X-pipeline SA token\nAdd OCP_SERVER + OCP_TOKEN_TEAM_X\nto GitHub Actions secrets]

    B -->|Existing team| E
    C4 --> E

    E{Where does\nthe server run?}

    E -->|In-cluster\ne.g. GitHub, DuckDuckGo| F[IT: Create MCPServer CR\nOperator deploys pod + Service]
    E -->|External SaaS\ne.g. Granola, Notion| G[No MCPServer CR needed\nGateway proxies to remote URL]

    F --> H[IT: Add visibility rule to\nmcpgateway.yaml\nserverName + role: mcp-team-X\neffect: allow]
    G --> H

    H --> I{Does the server\nuse OAuth?}

    I -->|Yes — SaaS with\nuser-level auth| J[IT: Add invokePrimordial allow rules\nfor NAME-authorize + NAME-revoke-auth\nwith role: mcp-team-X in mcpgateway.yaml]
    I -->|No| K

    J --> K[Team: Add server entry to\ncatalog-team-X.yaml\nPush via GHA pipeline]

    K --> L{How are upstream\ncredentials supplied?}

    L -->|OAuth PKCE\nSaaS handles auth| M[Team: Add oauth.providers block\ngateway creates NAME-authorize primordial]
    L -->|Per-user PAT\ne.g. GitHub| N[Team: Add auth_delegation: gateway\nOps loads NAME-pat-OID secrets\ninto Azure Key Vault per user]
    L -->|No auth needed\npublic server| O

    M --> O[IT: Restart CP deployment\nkubectl rollout restart deploy/mcp-gw-cp\nRequired to pick up catalog changes]
    N --> O

    O --> P{Does the team want\ntool-level restrictions?}

    P -->|Yes| Q[Team: Add deny rules to\nmanifests/team-X-policy.yaml\nPush via GHA pipeline\nSidecar picks up within ~60s\nno CP restart needed]
    P -->|No| R

    Q --> R([Server live for mcp-team-X members ✓])

    style A fill:#e8f4f8,stroke:#2196F3,color:#000000
    style R fill:#e8f5e9,stroke:#4CAF50,color:#000000
    style C1 fill:#fff3e0,stroke:#FF9800,color:#000000
    style C2 fill:#fff3e0,stroke:#FF9800,color:#000000
    style C3 fill:#fff3e0,stroke:#FF9800,color:#000000
    style C4 fill:#fff3e0,stroke:#FF9800,color:#000000
    style F fill:#fff3e0,stroke:#FF9800,color:#000000
    style H fill:#fff3e0,stroke:#FF9800,color:#000000
    style J fill:#fff3e0,stroke:#FF9800,color:#000000
    style O fill:#fff3e0,stroke:#FF9800,color:#000000
    style K fill:#f3e5f5,stroke:#9C27B0,color:#000000
    style M fill:#f3e5f5,stroke:#9C27B0,color:#000000
    style N fill:#f3e5f5,stroke:#9C27B0,color:#000000
    style Q fill:#f3e5f5,stroke:#9C27B0,color:#000000
```

## Actor legend

| Color | Owner | What they control |
|-------|-------|-------------------|
| Amber | IT / central ops | Entra setup, MCPGateway CR, MCPServer CRs, pipeline RBAC, CP restarts |
| Pink | Team | Catalog ConfigMap, policy ConfigMap, GHA pipeline pushes |

## Key rules

- **MCPGateway CR is flip-to-deny.** A server is invisible unless an explicit `allow` rule exists for it.
- **Catalog changes need a CP restart.** The control plane reads the catalog at startup.
- **Policy changes do not.** The sidecar re-reads `/etc/mcp-policy/` on every call (kubelet syncs the ConfigMap volume within ~60s).
- **Teams can only deny, not grant.** Their policy layer runs after the MCPGateway CR allows the request — it can add restrictions, not bypass server visibility.
