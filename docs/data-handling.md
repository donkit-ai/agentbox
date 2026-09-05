# Data handling in the open-source software

The SDK and MCP server send requests to the AgentBox API URL configured by the operator. They do not send
mailbox contents to AgentBox developers or an analytics service. Mailbox contents returned by a tool become
visible to the agent platform/model provider chosen by the user; review that platform's own retention policy.

Self-hosted AgentBox stores messages/attachments in the operator's database and object storage. Mailbox credentials
and OAuth refresh tokens are encrypted using the operator's application secret. OAuth connects to Google or
Microsoft; IMAP/SMTP connects to the mailbox's configured provider. Outbound email necessarily reaches the
selected mail provider and authorized recipients. Email may also be sent to operator-configured webhook URLs.

The local demo uses Mailpit. No cloud account is required, and the demo relay does not deliver to external mail
servers. The operator controls backups, retention, access, logging and deletion. This document describes software
behavior; it is not a hosted service's privacy agreement or a substitute for the operator's own policy.
