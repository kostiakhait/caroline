import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer as createHttpServer } from "node:http";
import { z } from "zod";

const systemTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;

function buildServer(): McpServer {
  const server = new McpServer({ name: "time", version: "1.0.0" });

  server.registerTool(
    "get_current_time",
    {
      title: "Get the current date and time",
      description:
        "Returns the current date and time. Use this whenever you need to know 'now' — the model has no built-in clock and must not guess. Defaults to the machine's local timezone; pass an IANA timezone (e.g. 'America/New_York', 'UTC') to get the time somewhere else instead.",
      inputSchema: {
        timezone: z
          .string()
          .optional()
          .describe(
            `IANA timezone name (e.g. "America/New_York", "Europe/London", "UTC"). Defaults to the machine's local timezone (${systemTimeZone}).`
          ),
      },
    },
    async ({ timezone }) => {
      const tz = timezone ?? systemTimeZone;
      const now = new Date();

      let formatted: string;
      try {
        formatted = new Intl.DateTimeFormat("en-US", {
          timeZone: tz,
          weekday: "long",
          year: "numeric",
          month: "long",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
          timeZoneName: "shortOffset",
        }).format(now);
      } catch (err) {
        return {
          isError: true,
          content: [
            {
              type: "text" as const,
              text: `Invalid timezone "${tz}": ${(err as Error).message}`,
            },
          ],
        };
      }

      const isoUtc = now.toISOString();

      return {
        content: [
          {
            type: "text" as const,
            text: `${formatted} (timezone: ${tz})\nISO 8601 (UTC): ${isoUtc}`,
          },
        ],
      };
    }
  );

  return server;
}

// --http-port <port> lets many query() instances (one per Caroline tab) share
// ONE running copy of this server instead of each spawning its own -- per
// explicit instruction (2026-09-06): stdio transport is strictly 1:1 (one
// parent, one child), so N tabs meant N independent copies of every such
// server, confirmed live as the actual cause of a 240+ node.exe process
// swarm accumulating over a day of restarts. Falls back to stdio when the
// flag is absent, for any caller (an interactive `claude` session's own
// project-scoped .mcp.json, for instance) that still expects to spawn its
// own copy.
const httpPortIdx = process.argv.indexOf("--http-port");
if (httpPortIdx >= 0) {
  const port = Number(process.argv[httpPortIdx + 1]);
  createHttpServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/mcp") {
      res.writeHead(404).end();
      return;
    }
    // A stateless transport (sessionIdGenerator: undefined) can only ever handle ONE
    // request -- reusing it, or the McpServer/Protocol it's connected to, across
    // requests throws ("Stateless transport cannot be reused across requests" /
    // "Already connected to a transport", both from the SDK itself). Confirmed live
    // (2026-09-06) as the actual cause of every one of Caroline's shared utility MCP
    // servers 500ing on every call past their very first, for hours, surviving app
    // restarts (a deterministic bug, not a stuck process). Fresh server+transport per
    // request, per the SDK's own stateless example
    // (examples/server/simpleStatelessStreamableHttp.js), fixes it.
    const server = buildServer();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res);
    } catch (err) {
      console.error("[mcp] request handling failed:", err);
      if (!res.headersSent) res.writeHead(500).end();
    }
    res.on("close", () => {
      transport.close();
      server.close();
    });
  }).listen(port, "127.0.0.1", () => {
    console.error(`[mcp] listening on http://127.0.0.1:${port}/mcp`);
  });
} else {
  const server = buildServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
