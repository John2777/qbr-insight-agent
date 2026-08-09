export const CONVERSATIONS_CHANGED_EVENT = "qbr-conversations-changed";

export function notifyConversationsChanged() {
  window.dispatchEvent(new Event(CONVERSATIONS_CHANGED_EVENT));
}
