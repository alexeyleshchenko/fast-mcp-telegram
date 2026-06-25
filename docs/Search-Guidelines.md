# 🔍 Search Guidelines

## Overview

Telegram search has specific limitations that AI models should understand to provide optimal search results.

## What Works ✅

- **Exact words**: `"deadline"`, `"meeting"`, `"project"`
- **Multiple terms**: `"deadline, meeting, project"` (comma-separated)
- **Partial words**: `"proj"` (finds "project", "projects", etc.)
- **Case insensitive**: `"DEADLINE"` finds "deadline", "Deadline", etc.
- **Sender filter**: `from_user` parameter filters by sender server-side (per-chat only)

## What Doesn't Work ❌

- **Wildcards**: `"proj*"`, `"meet%"`, `"dead*line"`
- **Regex patterns**: `"^project"`, `"deadline$"`, `"proj.*"`
- **Boolean operators**: `"project AND deadline"`, `"meeting OR call"`
- **Quotes for exact phrases**: `"exact phrase"` (treated as separate words)
- **Sender names in query**: `query="Букрей"` does NOT search sender names — use `from_user` instead

## Best Practices 💡

- Use simple, common words that are likely to appear in messages
- Try multiple related terms: `"deadline, due, urgent"`
- Use partial words for broader matches: `"proj"` instead of `"project*"`
- Start with specific terms and broaden if needed
- Use chat-specific search when possible for better results

## Search Examples

### Sender-Specific Searches

```json
// ✅ Good: Filter by sender name (server-side, per-chat only)
{"tool": "get_messages", "params": {"chat_id": "-1001234567890", "from_user": "alice", "limit": 20}}

// ✅ Good: Search text + sender filter
{"tool": "get_messages", "params": {"chat_id": "-1001234567890", "query": "docs.google.com", "from_user": "Букрей", "limit": 20}}

// ✅ Good: Browse messages from specific sender with date range
{"tool": "get_messages", "params": {"chat_id": "-1001234567890", "from_user": "@username", "min_date": "2025-01-01", "limit": 50}}

// ❌ Bad: Sender names in query don't work
{"tool": "get_messages", "params": {"chat_id": "-1001234567890", "query": "Букрей"}}

// ❌ Bad: from_user not supported in global search
{"tool": "search_messages_globally", "params": {"query": "hello", "from_user": "alice"}}
```

**Note:** `from_user` uses Telegram's native `from_id` filter — zero extra latency, server-side filtering. Only works with per-chat search (`get_messages` with `chat_id`). For global search, results must be filtered client-side.

**Note:** Both `chat_id` and `from_user` accept `-100` prefixed IDs (e.g., `"-1001234567890"`). The `-100` prefix is automatically stripped to resolve the raw channel/user ID. Also accepts t.me URLs, `"me"` for Saved Messages, and numeric IDs with multi-peer fallback.

### Global Search Examples

```json
// ✅ Good: Simple, common words
{"tool": "search_messages_globally", "params": {"query": "deadline", "limit": 20}}

// ✅ Good: Multiple related terms
{"tool": "search_messages_globally", "params": {"query": "deadline, due, urgent", "limit": 30}}

// ✅ Good: Partial word for broader matches
{"tool": "search_messages_globally", "params": {"query": "proj", "limit": 20}}

// ❌ Bad: Wildcards don't work
{"tool": "search_messages_globally", "params": {"query": "proj*"}}

// ❌ Bad: Regex patterns don't work
{"tool": "search_messages_globally", "params": {"query": "^project"}}

// ❌ Bad: Boolean operators don't work
{"tool": "search_messages_globally", "params": {"query": "project AND deadline"}}
```

### Chat-Specific Search Examples

```json
// ✅ Good: Search in specific chat
{"tool": "get_messages", "params": {"chat_id": "-1001234567890", "query": "launch"}}

// ✅ Good: Get latest messages (no query)
{"tool": "get_messages", "params": {"chat_id": "me", "limit": 10}}

// ✅ Good: Multi-term search in chat
{"tool": "get_messages", "params": {"chat_id": "telegram", "query": "update, news"}}

// ✅ Good: Partial word search
{"tool": "get_messages", "params": {"chat_id": "me", "query": "proj"}}
```

### Filtered Search Examples

```json
// ✅ Good: Filter by chat type
{"tool": "search_messages_globally", "params": {
  "query": "meeting",
  "chat_type": "private",
  "limit": 20
}}

// ✅ Good: Filter by date range
{"tool": "search_messages_globally", "params": {
  "query": "project",
  "min_date": "2025-01-01",
  "max_date": "2025-12-31",
  "limit": 30
}}

// ✅ Good: Combined filters
{"tool": "search_messages_globally", "params": {
  "query": "deadline, urgent",
  "chat_type": "private",
  "min_date": "2025-01-01",
  "limit": 15
}}
```

## Search Strategy for AI Models

### 1. Start Specific, Then Broaden
```json
// Start with specific term
{"tool": "search_messages_globally", "params": {"query": "deadline", "limit": 10}}

// If no results, try related terms
{"tool": "search_messages_globally", "params": {"query": "deadline, due, urgent", "limit": 20}}

// If still no results, try partial words
{"tool": "search_messages_globally", "params": {"query": "dead", "limit": 20}}
```

### 2. Use Chat-Specific Search When Possible
```json
// If user mentions a specific person/channel, search there first
{"tool": "get_messages", "params": {"chat_id": "@username", "query": "project"}}

// Then try global search if needed
{"tool": "search_messages_globally", "params": {"query": "project", "limit": 20}}
```

### 3. Apply Filters Strategically
```json
// Use date filters to narrow results
{"tool": "search_messages_globally", "params": {
  "query": "meeting",
  "min_date": "2025-01-01",
  "limit": 20
}}

// Use chat type filters for targeted results
{"tool": "search_messages_globally", "params": {
  "query": "announcement",
  "chat_type": "channel",
  "limit": 15
}}
```

## Common Search Patterns

### Finding Messages by Content
```json
// Look for specific topics
{"tool": "search_messages_globally", "params": {"query": "budget, finance, money", "limit": 20}}

// Look for time-related messages
{"tool": "search_messages_globally", "params": {"query": "tomorrow, next week, deadline", "limit": 20}}

// Look for action items
{"tool": "search_messages_globally", "params": {"query": "todo, task, action", "limit": 20}}
```

### Finding Messages by Context
```json
// Recent messages from a specific person
{"tool": "get_messages", "params": {"chat_id": "@username", "limit": 10}}

// Messages in a specific time period
{"tool": "search_messages_globally", "params": {
  "query": "project",
  "min_date": "2025-01-01",
  "max_date": "2025-01-31",
  "limit": 30
}}

// Messages in specific chat types
{"tool": "search_messages_globally", "params": {
  "query": "announcement",
  "chat_type": "channel",
  "limit": 20
}}
```

## Performance Tips

### Limit Management
- Start with small limits (10-20) for initial exploration
- Increase limits only if needed and results are relevant
- Use filters to narrow results before increasing limits
- Avoid requesting more than 50 results in a single search

### Query Optimization
- Use common words that are likely to appear in messages
- Try multiple related terms with comma separation
- Use partial words for broader matches
- Avoid complex search patterns that don't work

### Result Processing
- Check if results are relevant before requesting more
- Use the most specific search that returns useful results
- Combine multiple searches if needed rather than one large search
- Consider chat-specific searches for better targeting

## Troubleshooting

### No Results Found
1. Try simpler, more common words
2. Use partial words for broader matches
3. Try related terms with comma separation
4. Check if the search should be chat-specific
5. Verify the search terms are likely to appear in messages

### Too Many Results
1. Add date filters to narrow the time range
2. Use chat type filters for more targeted results
3. Use more specific search terms
4. Reduce the limit parameter
5. Try chat-specific search instead of global search

### Irrelevant Results
1. Use more specific search terms
2. Add filters (date, chat type) to narrow results
3. Try chat-specific search for better targeting
4. Use multiple related terms with comma separation
5. Consider the context and use appropriate search strategy
