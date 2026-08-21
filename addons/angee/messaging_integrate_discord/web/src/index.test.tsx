import { CHANNEL_MODEL } from "@angee/messaging";
import { expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateDiscord from "./index";

describe("messaging_integrate_discord addon manifest", () => {
  test("declares a live bridge with no QR instruction", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateDiscord)).not.toThrow();
    const actions = (messagingIntegrateDiscord.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "discord")),
    );
  });

  test("states the bot's guild-scoped visibility wall", () => {
    expect(messagingIntegrateDiscord.i18n?.messaging?.["channel.discord.description"]).toBe(
      "Discord ingests the servers you invite the bot to; it cannot read your private DMs.",
    );
  });
});
