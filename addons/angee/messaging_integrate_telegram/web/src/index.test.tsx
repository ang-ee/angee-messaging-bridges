import { expectChannelVerbsScoped, expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { describe, expect, test } from "vitest";

import messagingIntegrateTelegram from "./index";

describe("messaging_integrate_telegram addon manifest", () => {
  test("declares a valid bridge at the Telegram implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateTelegram)).not.toThrow();
    expect(() => expectChannelVerbsScoped(messagingIntegrateTelegram, "telegram")).not.toThrow();
  });

  test("contributes Telegram application-key copy", () => {
    expect(messagingIntegrateTelegram.i18n?.messaging?.["channel.telegram.scan"]).toContain(
      "Link Desktop Device",
    );
    expect(messagingIntegrateTelegram.i18n?.messaging?.["channel.telegram.keysHelp"]).toContain(
      "application keys",
    );
  });
});
