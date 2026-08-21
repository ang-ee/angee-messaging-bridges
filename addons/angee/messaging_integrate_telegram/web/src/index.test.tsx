import { expectValidBaseAddon } from "@angee/app/testing";
import { CHANNEL_MODEL, MESSAGING_CHANNEL_TOOLBAR_SLOT } from "@angee/messaging";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateTelegram from "./index";

describe("messaging_integrate_telegram addon manifest", () => {
  test("declares a valid bridge at the Telegram implementation key", () => {
    expect(() => expectValidBaseAddon(messagingIntegrateTelegram)).not.toThrow();
    expect(messagingIntegrateTelegram.slots?.[0]).toMatchObject({
      slot: MESSAGING_CHANNEL_TOOLBAR_SLOT,
      id: "messaging-integrate-telegram.connect",
      sequence: 21,
    });
    const actions = (messagingIntegrateTelegram.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "telegram")),
    );
  });

  test("contributes Telegram navigation and application-key copy", () => {
    expect(messagingIntegrateTelegram.menus?.[0]).toMatchObject({
      id: "messaging.telegram",
      label: "Telegram",
      parentId: "messaging",
      description: "Link Telegram accounts by QR code",
    });
    expect(messagingIntegrateTelegram.i18n?.messaging?.["channel.telegram.scan"]).toContain(
      "Link Desktop Device",
    );
    expect(messagingIntegrateTelegram.i18n?.messaging?.["channel.telegram.keysHelp"]).toContain(
      "application keys",
    );
  });
});
