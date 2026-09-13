## Privacy Policy for Eval Loop

This plugin reads a Dify chat app's own conversation logs and evaluates them for quality. It does not modify, delete, or create anything in your Dify workspace.

---

**Data Collection**

To function, this plugin requires:

- **A Dify app API key**: used solely to call that app's own Service API (`/conversations`, `/messages`) to read conversation logs.
- **A target model selection**: the model you configure is used to judge each Q&A pair.
- **Target end-user identifiers**: you specify which Dify end users' conversations to pull, since Dify's API scopes conversation access per end user.

While running, the plugin reads:

- **Questions and answers** from the selected app's conversation history, within the configured lookback window.
- **User feedback** (like/dislike ratings), where already recorded on a message.
- **Retrieved knowledge-base passages**, where the app has retrieval enabled and Dify has attached them to a message.

---

**Data Usage**

- **API key**: used only to authenticate requests to the linked Dify app's own Service API. Stored as an encrypted plugin credential by Dify; never sent anywhere else.
- **Conversation content**: question/answer text, feedback ratings, and retrieved passages are sent to the model you configure as the "Eval Model," so it can judge groundedness, relevance, correctness, and reusability.
- **Data Retention**: this plugin does not persist any data itself. Each run reads live logs from your Dify app and returns a report in its HTTP response; nothing is cached or stored by the plugin between runs.

---

**Third-Party Services**

- The plugin communicates only with the Dify instance you configure (via its Service API) and with the model provider backing your chosen Eval Model, both of which you control and configure. It does not send data to any other third party.
