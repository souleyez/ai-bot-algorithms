# AI-BOT reviewed truth connector

Reads only frozen, exact-revision human truth snapshots from the loopback review service. Credentials arrive through managed-process file descriptor 3; cursor state contains no credential. Publication acknowledgement is performed only by the separate coordinator after an exact DataMax SourceVersion receipt.
