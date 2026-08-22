// @vitest-environment happy-dom

import { cleanup, render, screen } from "@testing-library/react";
import * as React from "react";
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

import {
  ConnectDiscordChannelAction,
  discordBotInviteUrl,
} from "./ConnectDiscordChannelAction";

describe("ConnectDiscordChannelAction", () => {
  afterEach(cleanup);

  beforeEach(() => {
    actionMocks.props = null;
  });

  test("declares typed bot variables and its pairing invite", () => {
    render(<ConnectDiscordChannelAction />);
    const fields = actionMocks.props?.fields as (
      t: (key: string) => string,
    ) => readonly { name: string }[];
    const parseValues = actionMocks.props?.parseValues as (
      values: Record<string, unknown>,
      t: (key: string) => string,
    ) => { applicationId: string; name: string; token: string };
    const nextStep = actionMocks.props?.nextStep as (
      values: Record<string, string>,
      t: (key: string) => string,
    ) => React.ReactNode;
    const variables = actionMocks.props?.variables as (
      values: { applicationId: string; name: string; token: string },
    ) => unknown;

    expect(actionMocks.props).toMatchObject({
      kind: "pairing",
      i18nPrefix: "channel.discord",
      resultField: "connect_discord_channel",
    });
    expect(fields((key) => key).map((field) => field.name)).toEqual([
      "name",
      "application_id",
      "token",
    ]);
    const values = parseValues(
      {
        name: " Community bot ",
        application_id: "123456789012345678",
        token: " bot-token ",
      },
      (key) => key,
    );
    expect(values).toEqual({
      name: "Community bot",
      applicationId: "123456789012345678",
      token: "bot-token",
    });
    expect(variables(values)).toEqual({
      name: "Community bot",
      token: "bot-token",
    });
    render(<>{nextStep(values, (key) => key)}</>);
    expect(
      screen.getByRole("link", { name: "channel.discord.invite" }).getAttribute("href"),
    ).toBe(
      "https://discord.com/oauth2/authorize?client_id=123456789012345678&scope=bot&permissions=66560",
    );
  });

  test("rejects malformed application ids before the shared mutation runs", () => {
    render(<ConnectDiscordChannelAction />);
    const parseValues = actionMocks.props?.parseValues as (
      values: Record<string, unknown>,
      t: (key: string) => string,
    ) => unknown;
    expect(() =>
      parseValues(
        { name: "Bot", application_id: "invalid", token: "token" },
        (key) => key,
      ),
    ).toThrow("channel.discord.applicationIdInvalid");
    expect(() => discordBotInviteUrl("123")).toThrow(/17–20 digit snowflake/);
  });
});
