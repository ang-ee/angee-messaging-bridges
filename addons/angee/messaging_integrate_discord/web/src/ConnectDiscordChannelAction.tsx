import {
  ConnectChannelAction,
  type ConnectChannelFields,
} from "@angee/messaging";
import type { AuthoredVariables } from "@angee/refine";
import {
  mutationDialogValueCodecs,
  type MutationDialogValues,
} from "@angee/ui";
import * as React from "react";

import { ConnectDiscordChannel } from "./documents";

const INVITE_PERMISSIONS = "66560";
const APPLICATION_ID_PATTERN = /^\d{17,20}$/;

interface DiscordConnectValues
  extends AuthoredVariables<typeof ConnectDiscordChannel> {
  applicationId: string;
}

const fields: ConnectChannelFields = (t) => [
  {
    name: "name",
    label: t("channel.connect.name"),
    placeholder: t("channel.discord.namePlaceholder"),
    required: true,
  },
  {
    name: "application_id",
    label: t("channel.discord.applicationId"),
    placeholder: t("channel.discord.applicationIdPlaceholder"),
    required: true,
    description: t("channel.discord.applicationIdHelp"),
  },
  {
    name: "token",
    label: t("channel.discord.token"),
    widget: "password",
    required: true,
    description: t("channel.discord.tokenHelp"),
  },
];

/** Discord field declaration followed by pairing and its bot-invite next step. */
export function ConnectDiscordChannelAction(): React.ReactElement {
  return (
    <ConnectChannelAction
      kind="pairing"
      document={ConnectDiscordChannel}
      fields={fields}
      i18nPrefix="channel.discord"
      parseValues={parseValues}
      variables={discordVariables}
      resultField="connect_discord_channel"
      nextStep={nextStep}
    />
  );
}

function discordVariables({
  name,
  token,
}: DiscordConnectValues): AuthoredVariables<typeof ConnectDiscordChannel> {
  return { name, token };
}

function parseValues(
  values: MutationDialogValues,
  t: (key: string) => string,
): DiscordConnectValues {
  const applicationId = mutationDialogValueCodecs.requiredString(
    values.application_id,
    "application_id",
  );
  if (!APPLICATION_ID_PATTERN.test(applicationId)) {
    throw new TypeError(t("channel.discord.applicationIdInvalid"));
  }
  return {
    name: mutationDialogValueCodecs.requiredString(values.name, "name"),
    token: mutationDialogValueCodecs.requiredString(values.token, "token"),
    applicationId,
  };
}

function nextStep(
  values: DiscordConnectValues,
  t: (key: string) => string,
): React.ReactNode {
  return (
    <p>
      <a
        href={discordBotInviteUrl(values.applicationId)}
        target="_blank"
        rel="noreferrer"
      >
        {t("channel.discord.invite")}
      </a>
    </p>
  );
}

export function discordBotInviteUrl(applicationId: string): string {
  if (!APPLICATION_ID_PATTERN.test(applicationId)) {
    throw new TypeError("A Discord application ID must be a 17–20 digit snowflake.");
  }
  return `https://discord.com/oauth2/authorize?client_id=${encodeURIComponent(applicationId)}&scope=bot&permissions=${INVITE_PERMISSIONS}`;
}
