# Onboarding a New MCP Server

Decision flow for adding a server under the group-based access model.
**IT** owns the MCPGateway CR and MCPServer CRs. **Teams** own their catalog and policy ConfigMaps.

```mermaid
flowchart TD
    A([New MCP server request]) --> B{In-cluster\nor external SaaS?}

    B -->|In-cluster\ne.g. GitHub, DuckDuckGo| C[IT: Create MCPServer CR\nOperator deploys pod + Service]
    B -->|External SaaS\ne.g. Granola, Notion| D[No MCPServer CR needed\nGateway proxies to remote URL]

    C --> E{Which team\ngets access?}
    D --> E

    E --> F[IT: Add visibility rule to\nmcpgateway.yaml\nserverName + role: mcp-team-X\neffect: allow]

    F --> G{Does the server\nuse OAuth?}

    G -->|Yes — SaaS with\nuser-level auth| H[IT: Add invokePrimordial allow rules\nfor NAME-authorize + NAME-revoke-auth\nwith role: mcp-team-X in mcpgateway.yaml]
    G -->|No| I

    H --> I[Team: Add server entry to\ncatalog-team-X.yaml\nPush via GHA pipeline]

    I --> J{How are upstream\ncredentials supplied?}

    J -->|OAuth PKCE\nSaaS handles auth| K[Add oauth.providers block\ngateway creates NAME-authorize primordial]
    J -->|Per-user PAT\ne.g. GitHub| L[Add auth_delegation: gateway\nOps loads NAME-pat-OID secrets\ninto Azure Key Vault per user]
    J -->|No auth needed\npublic server| M

    K --> M[IT: Restart CP deployment\nkubectl rollout restart deploy/mcp-gw-cp\nRequired to pick up catalog changes]
    L --> M

    M --> N{Does the team want\ntool-level restrictions?}

    N -->|Yes| O[Team: Add deny rules to\nmanifests/team-a-policy.yaml\nPush via GHA pipeline\nSidecar picks up within ~60s\nno CP restart needed]
    N -->|No| P

    O --> P([Server live for mcp-team-X members ✓])

    style A fill:#e8f4f8,stroke:#2196F3
    style P fill:#e8f5e9,stroke:#4CAF50
    style C fill:#fff3e0,stroke:#FF9800
    style F fill:#fff3e0,stroke:#FF9800
    style H fill:#fff3e0,stroke:#FF9800
    style M fill:#fff3e0,stroke:#FF9800
    style I fill:#f3e5f5,stroke:#9C27B0
    style K fill:#f3e5f5,stroke:#9C27B0
    style L fill:#f3e5f5,stroke:#9C27B0
    style O fill:#f3e5f5,stroke:#9C27B0
```

## Actor legend

| Color | Owner | What they control |
|-------|-------|-------------------|
| Orange | IT / central ops | MCPGateway CR, MCPServer CRs, CP restarts |
| Purple | Team | Catalog ConfigMap, policy ConfigMap, GHA pipeline |

## Key rules

- **MCPGateway CR is flip-to-deny.** A server is invisible unless an explicit `allow` rule exists for it.
- **Catalog changes need a CP restart.** The control plane reads the catalog at startup.
- **Policy changes do not.** The sidecar re-reads `/etc/mcp-policy/` on every call (kubelet syncs the ConfigMap volume within ~60s).
- **Teams can only deny, not grant.** Their policy layer runs after the MCPGateway CR allows the request — it can add restrictions, not bypass server visibility.
