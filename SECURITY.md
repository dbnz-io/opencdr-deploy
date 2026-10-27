# Security policy

Please report vulnerabilities privately through GitHub's security-advisory
interface for `dbnz-io/opencdr-deploy`. Do not open a public issue containing
credentials, customer identifiers, deployment manifests, or exploit details.

The deployment registry must never contain AWS credentials, API keys, webhook
URLs, tokens, or passwords. The CLI rejects secret-shaped Serverless parameter
names, reads AWS credentials from the standard AWS chain, and retrieves OpenCDR
API credentials from SSM only when needed.

Before attaching logs to an issue, review them for AWS account IDs, ARNs,
repository paths, and output from Core operational scripts. The manager conceals
script arguments, but it cannot sanitize output produced by an invoked script.
