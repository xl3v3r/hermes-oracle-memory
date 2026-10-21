## 2024-05-24 - Avoid O(N) Iteration on the messages Array
**Learning:** Agent conversations can grow extremely long, and performing O(N) traversals over the entire `messages` array in hooks like `on_session_end` or `sync_turn` scales poorly and can block the main thread.
**Action:** When extracting recent messages, iterate backwards over the list using `reversed(messages)` and break out early once the required number of messages has been found, turning an O(N) scan into O(1).
