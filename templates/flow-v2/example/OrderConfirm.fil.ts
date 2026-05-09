async function main(): Promise<void> {
  const emailsRaw = await executeNode(
    "uipath.connector.uipath-microsoft-outlook365.get-email-list",
    JSON.stringify({ parentFolderId: "inbox", unReadOnly: true })
  );
  const emails = JSON.parse(emailsRaw);
  if (emails.length > 0) {
    const subject = (emails[0].subject ?? "(no subject)");
    await executeNode(
      "uipath.connector.uipath-salesforce-slack.send-message-to-channel",
      JSON.stringify({
        send_as: "bot",
        channel: "#alerts",
        messageToSend: ("New email: " + subject)
      })
    );
  }
}
