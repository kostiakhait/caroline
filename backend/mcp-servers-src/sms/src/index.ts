import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";
import { login, withSession } from "./session.js";
import { smsSend, smsViewReceived, smsSetAccount, smsGetAccount, smsRemoveAccount } from "./api.js";

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function jsonResult(data: unknown) {
  return textResult(JSON.stringify(data, null, 2));
}

function buildServer(): McpServer {
  const server = new McpServer({ name: "sms", version: "1.0.0" });

  server.registerTool(
    "sms_login",
    {
      title: "Log in to SquirrelWisdom",
      description:
        "One-time login with the user's SquirrelWisdom email/password. Verifies the credentials, saves " +
        "them locally (~/.mcp-notes/credentials.json -- the SAME file Notes uses, one shared login) so " +
        "the server can silently re-login on every future start, and establishes the account for this " +
        "process. Call again to switch accounts. NEVER needed if this machine is already logged in via " +
        "Notes or Caroline -- try sms_get_account_status first.",
      inputSchema: {
        email: z.string().email().describe("SquirrelWisdom account email."),
        password: z.string().describe("SquirrelWisdom account password."),
      },
    },
    async ({ email, password }) => {
      await login(email, password);
      return textResult(`Logged in as ${email}.`);
    },
  );

  server.registerTool(
    "sms_set_account",
    {
      title: "Register your own SMTP2GO account for SMS",
      description:
        "Registers YOUR OWN SMTP2GO account (your own API key, optionally your own dedicated sending " +
        "number) for sms_send/sms_view_received to use -- per design, every user sends/receives through " +
        "their own account, not a shared one. The key is verified against SMTP2GO's real API before being " +
        "saved (an invalid key is rejected, nothing persisted). Get an API key at " +
        "app.smtp2go.com (or app-eu/app-au for other regions) -> Sending -> API Keys. Overwrites any " +
        "previously registered account for this user.",
      inputSchema: {
        smtp2go_api_key: z.string().describe('Your SMTP2GO API key, e.g. "api-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX".'),
        smtp2go_sender: z.string().optional().describe(
          "Your dedicated sending number in E.164 format (e.g. \"+12025550123\"), if you have one. Omit to " +
            "let SMTP2GO pick an appropriate shared number per message instead."
        ),
      },
    },
    async ({ smtp2go_api_key, smtp2go_sender }) => {
      try {
        await withSession((session) => smsSetAccount(session, smtp2go_api_key, smtp2go_sender));
      } catch (err) {
        return { content: [{ type: "text", text: `Could not register account: ${err instanceof Error ? err.message : String(err)}` }], isError: true };
      }
      return textResult("SMTP2GO account registered.");
    },
  );

  server.registerTool(
    "sms_get_account_status",
    {
      title: "Check your registered SMTP2GO account",
      description: "Reports whether you have a registered SMTP2GO account and its sender number, if set. Never reveals the API key itself.",
      inputSchema: {},
    },
    async () => {
      const status = await withSession((session) => smsGetAccount(session));
      return jsonResult(status);
    },
  );

  server.registerTool(
    "sms_remove_account",
    {
      title: "Remove your registered SMTP2GO account",
      description: "Deletes your registered SMTP2GO account. sms_send/sms_view_received will fail until you register a new one with sms_set_account.",
      inputSchema: {},
    },
    async () => {
      await withSession((session) => smsRemoveAccount(session));
      return textResult("SMTP2GO account removed.");
    },
  );

  server.registerTool(
    "sms_send",
    {
      title: "Send an SMS",
      description:
        "Sends an SMS to one or more numbers (E.164 format) via YOUR OWN registered SMTP2GO account (see " +
        "sms_set_account -- fails with a clear error if you haven't registered one yet). Costs real money " +
        "on your own SMTP2GO account.",
      inputSchema: {
        destination: z.union([z.string(), z.array(z.string())]).describe("One E.164 number, or an array of up to 100."),
        content: z.string().describe("Message text."),
        sender: z.string().optional().describe("Override sender number for just this message (defaults to your registered sender, or an SMTP2GO shared number)."),
      },
    },
    async ({ destination, content, sender }) => {
      try {
        const result = await withSession((session) => smsSend(session, destination, content, sender));
        return jsonResult(result);
      } catch (err) {
        return { content: [{ type: "text", text: `Send failed: ${err instanceof Error ? err.message : String(err)}` }], isError: true };
      }
    },
  );

  server.registerTool(
    "sms_view_received",
    {
      title: "View received SMS replies",
      description:
        "Lists replies received on YOUR OWN registered SMTP2GO account since start_date (defaults to the " +
        "last 7 days). This is polling, not a live inbox -- SMTP2GO's 'received' messages are replies to " +
        "SMS you sent, not general inbound texts to an arbitrary number.",
      inputSchema: {
        start_date: z.string().optional().describe("ISO-8601 datetime, defaults to 7 days ago."),
        end_date: z.string().optional().describe("ISO-8601 datetime, defaults to now."),
      },
    },
    async ({ start_date, end_date }) => {
      const messages = await withSession((session) => smsViewReceived(session, start_date, end_date));
      return jsonResult(messages);
    },
  );

  return server;
}

// --http-port <port> lets many query() instances (one per Caroline tab)
// share ONE running copy of this server instead of each spawning its own --
// same reasoning/shape as MCP/notes' index.ts (see its own comment: stdio
// is strictly 1:1, and a fresh server+transport per request avoids the SDK's
// "stateless transport cannot be reused" error).
const httpPortIdx = process.argv.indexOf("--http-port");
if (httpPortIdx >= 0) {
  const port = Number(process.argv[httpPortIdx + 1]);
  createHttpServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/mcp") {
      res.writeHead(404).end();
      return;
    }
    const server = buildServer();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res);
    } catch (err) {
      console.error("[mcp-sms] request handling failed:", err);
      if (!res.headersSent) res.writeHead(500).end();
    }
    res.on("close", () => {
      transport.close();
      server.close();
    });
  }).listen(port, "127.0.0.1", () => {
    console.error(`[mcp-sms] listening on http://127.0.0.1:${port}/mcp`);
  });
} else {
  const server = buildServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
