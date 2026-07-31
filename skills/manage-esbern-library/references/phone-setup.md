# Phone setup

Both integrations use the same hosted Esbern library but require a one-time
setup in a desktop browser. After setup, use the resulting custom GPT or Claude
connector from the iOS or Android app.

## ChatGPT

1. Open the GPT editor in a browser and create a private custom GPT.
2. Paste the contents of `assistant-instructions.md` into Instructions.
3. Under Actions, create an action and import this live schema URL:
   `https://esbern.rishi.cx/integrations/chatgpt/openapi.json`
4. Set Authentication to API Key, choose Bearer, and paste the value returned
   by this command on the owner's computer:

   ```sh
   ssh railway 'cat /etc/esbern/api-token'
   ```

5. Save it privately. Open that GPT in the ChatGPT phone app and ask naturally,
   for example, “Add *The Left Hand of Darkness* by Ursula K. Le Guin.”

The same OpenAPI document is also saved as `chatgpt-action.yaml` for inspection
or manual import.

## Claude

1. On claude.ai, open Settings, then Connectors, and add a custom connector
   named `Esbern Library`.
2. Use the URL returned by this command on the owner's computer:

   ```sh
   ssh railway 'cat /etc/esbern/claude-connector-url'
   ```

3. Enable the connector in a conversation. Its tools and safety instructions
   are supplied by the server. The connector can then be used from Claude on
   iOS or Android.

Treat both values as passwords. If either leaks, rotate it on the server and
replace it in the corresponding assistant configuration.
