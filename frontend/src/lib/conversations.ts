/**
 * Past conversations: the signed-in user's saved chats.
 *
 * The backend only ever returns the caller's own chats. A chat that doesn't
 * exist, belongs to someone else, or was hidden all answer 404 alike, so the UI
 * treats a 404 as "this chat is gone" whatever the cause.
 */

import { authFetch } from './auth';

export type ConversationSummary = {
  id: string;
  title: string;
  created_at: string;
  last_used: string;
};

export type ConversationMessage = { role: 'user' | 'assistant'; content: string };

export type Conversation = ConversationSummary & { messages: ConversationMessage[] };

/** An error reply from the API; `status` lets callers react to 401 and 404. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await authFetch(path, init);
  } catch {
    throw new ApiError('Unable to reach the VoxPath backend.', 0);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = typeof body?.detail === 'string' ? body.detail : typeof body?.error === 'string' ? body.error : null;
    throw new ApiError(detail || `Request failed (HTTP ${res.status}).`, res.status);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

/** The user's chats, most recently used first. */
export const listConversations = (limit: number, offset = 0) =>
  request<ConversationSummary[]>(`/conversations?limit=${limit}&offset=${offset}`);

/** One chat with its messages, as the user saw them. */
export const openConversation = (id: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}`);

/** Hide a chat from the app. It stays in the database. */
export const hideConversation = (id: string) =>
  request<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
