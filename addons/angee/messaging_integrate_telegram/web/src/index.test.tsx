import { CHANNEL_MODEL } from "@angee/messaging";
import { expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateTelegram from "./index";

describe("messaging_integrate_telegram addon manifest", () => {
  test("declares a valid bridge at the Telegram implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateTelegram)).not.toThrow();
    const actions = (messagingIntegrateTelegram.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "telegram")),
    );
  });

  test("contributes Telegram navigation and application-key copy", () => {
    expect(messagingIntegrateTelegram.menus?.[0]?.description).toBe("Link Telegram accounts by QR code");
    expect(messagingIntegrateTelegram.i18n?.messaging?.["channel.telegram.scan"]).toContain(
      "Link Desktop Device",
    );
    expect(messagingIntegrateTelegram.i18n?.messaging?.["channel.telegram.keysHelp"]).toContain(
      "application keys",
    );
  });
});
