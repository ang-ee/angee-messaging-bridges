// @vitest-environment happy-dom

import { cleanup, render, screen } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const actionMocks = vi.hoisted(() => ({
  props: null as Record<string, unknown> | null,
  createKeys: vi.fn(async () => ({
    create_credential: { id: "cred_9", display_name: "My keys" },
  })),
}));

vi.mock("./documents", () => ({
  ConnectTelegramChannel: "ConnectTelegramChannel",
  CreateTelegramAppKeys: "CreateTelegramAppKeys",
}));

vi.mock("@angee/refine", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@angee/refine")>()),
  useAuthoredMutation: (document: unknown) => {
    expect(document).toBe("CreateTelegramAppKeys");
    return [actionMocks.createKeys];
  },
}));

vi.mock("@angee/messaging", () => ({
  ConnectChannelAction: (props: Record<string, unknown>) => {
    actionMocks.props = props;
    return <button type="button">connect</button>;
  },
}));

import { ConnectTelegramChannelAction } from "./ConnectTelegramChannelAction";

interface DialogField {
  name: string;
  relation?: {
    resource: string;
    filters?: readonly unknown[];
    create?: {
      fields?: readonly { name: string; kind?: string; widget?: string; description?: React.ReactNode }[];
      submit: (values: Record<string, unknown>) => Promise<unknown>;
    };
  };
}

function declaredFields(): readonly DialogField[] {
  const fields = actionMocks.props?.fields as (
    t: (key: string) => string,
  ) => readonly DialogField[];
  return fields((key) => key);
}

describe("ConnectTelegramChannelAction", () => {
  afterEach(cleanup);

  beforeEach(() => {
    actionMocks.props = null;
    actionMocks.createKeys.mockClear();
  });

  test("declares the credential selection and typed pairing variables", () => {
    render(<ConnectTelegramChannelAction />);
    const fields = declaredFields();
    const parseValues = actionMocks.props?.parseValues as (
      values: Record<string, unknown>,
    ) => unknown;

    expect(actionMocks.props).toMatchObject({
      kind: "pairing",
      i18nPrefix: "channel.telegram",
      resultField: "connect_telegram_channel",
      instructionKey: "channel.telegram.scan",
    });
    expect(fields).toMatchObject([
      { name: "name" },
      {
        name: "credential",
        relation: {
          resource: "integrate.Credential",
          filters: [{ field: "kind", operator: "eq", value: "app_keys" }],
        },
      },
    ]);
    expect(parseValues({ name: " Ada Telegram ", credential: "cred_9" })).toEqual({
      name: "Ada Telegram",
      credentialId: "cred_9",
    });
  });

  test("keeps inline Telegram app-key creation with the relation owner", async () => {
    render(<ConnectTelegramChannelAction />);
    const create = declaredFields().find((field) => field.name === "credential")
      ?.relation?.create;

    expect(create?.fields).toMatchObject([
      { name: "name" },
      { name: "app_id", kind: "integer" },
      { name: "app_secret", widget: "password" },
    ]);
    const row = await create?.submit({
      name: " My keys ",
      app_id: 123456,
      app_secret: " telegram-api-hash ",
    });
    expect(actionMocks.createKeys).toHaveBeenCalledWith({
      name: "My keys",
      appId: "123456",
      appSecret: "telegram-api-hash",
    });
    expect(row).toEqual({ id: "cred_9", display_name: "My keys" });

    const secret = create?.fields?.find((field) => field.name === "app_secret");
    render(<>{secret?.description}</>);
    expect(
      screen
        .getByRole("link", { name: "channel.telegram.keysLink" })
        .getAttribute("href"),
    ).toBe("https://my.telegram.org/");
  });
});
