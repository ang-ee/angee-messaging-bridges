// @vitest-environment happy-dom

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const actionMocks = vi.hoisted(() => ({
  props: null as Record<string, unknown> | null,
}));

vi.mock("@angee/messaging", () => ({
  ConnectChannelAction: (props: Record<string, unknown>) => {
    actionMocks.props = props;
    return <button type="button">connect</button>;
  },
}));

import { ConnectWhatsappChannelAction } from "./ConnectWhatsappChannelAction";

describe("ConnectWhatsappChannelAction", () => {
  afterEach(cleanup);

  beforeEach(() => {
    actionMocks.props = null;
  });

  test("declares a trimmed name and QR pairing through the factory", () => {
    render(<ConnectWhatsappChannelAction />);
    const fields = actionMocks.props?.fields as (
      t: (key: string) => string,
    ) => readonly { name: string }[];
    const parseValues = actionMocks.props?.parseValues as (
      values: Record<string, unknown>,
    ) => unknown;

    expect(actionMocks.props).toMatchObject({
      kind: "pairing",
      i18nPrefix: "channel.whatsapp",
      resultField: "connect_whatsapp_channel",
      instructionKey: "channel.whatsapp.scan",
    });
    expect(fields((key) => key).map((field) => field.name)).toEqual(["name"]);
    expect(parseValues({ name: " Personal WhatsApp " })).toEqual({
      name: "Personal WhatsApp",
    });
  });
});
