import { expectChannelVerbsScoped, expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { describe, expect, test } from "vitest";

import messagingIntegrateDiscord from "./index";

describe("messaging_integrate_discord addon manifest", () => {
  test("declares a live bridge with no QR instruction", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateDiscord)).not.toThrow();
    expect(() => expectChannelVerbsScoped(messagingIntegrateDiscord, "discord")).not.toThrow();
  });

  test("states the bot's guild-scoped visibility wall", () => {
    expect(messagingIntegrateDiscord.i18n?.messaging?.["channel.discord.description"]).toBe(
      "Discord ingests the servers you invite the bot to; it cannot read your private DMs.",
    );
  });
});
