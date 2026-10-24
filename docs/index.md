+++
title = "Oaken Mail"
subtitle = "A local-first, AI-native mail client"
lead = "Oaken Mail is a mail client that keeps your mail on your own machine. It pulls messages to local disk, reads them with a local language model and embedder as they arrive, and stores them so that search and question answering stay fast and cheap. Nothing leaves your computer unless you choose to point it at a remote service."
badges = [["text-bg-warning", "Beta"], ["text-bg-secondary", "Local-first"], ["text-bg-primary", "Open source"]]
description = "A local-first, AI-native mail client: your mail, processed and searched on your own machine."
+++

## What it is for

- **A local MCP server for sensitive mail.** Core hosts an MCP server over your local mail store, so a more capable model can search, read and act on your mail through a narrow, audited interface without the mail being uploaded anywhere. Sending is off unless you turn it on.
- **Your mail stays yours.** Messages are stored on your disk. The original bytes are kept, so nothing is lost and everything derived from them can be rebuilt.
- **Ask your mailbox questions.** Search combines keywords and meaning, and answers cite the messages they came from.
- **Cheap by design.** Mail is cleaned once when it arrives (quoted replies, signatures and boilerplate removed), and every model result is cached, so asking a question sends only the few passages that matter.
- **Mail is data, not instructions.** Text from a message is never treated as a command, and anything that changes your mail has to be confirmed.
- **Extensible.** Mail providers, embedding models, language models and enrichers are plugins behind small, declared contracts.

::: callout
**Where things stand.** Oaken Mail is in beta (0.1.x) and is built in the open. The engine, the CLI, the MCP server and the Gmail, IMAP and SMTP providers work; install it with `pip install oaken-core`. Sending mail, the contacts view and several settings screens are still to come. Start with [Set up Oaken from scratch](getting-started.md).
:::

## The documents

{{doc-cards}}
