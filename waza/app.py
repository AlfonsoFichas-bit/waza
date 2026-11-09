import sys
import os
import sqlite3
import time
from typing import Optional, List
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, ListView, ListItem, Label, Input, Static
from textual.reactive import reactive
from textual import work
from textual.message import Message

# Add the whatsapp-mcp-server to sys.path to import its modules
WHATSAPP_SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-mcp', 'whatsapp-mcp-server')
sys.path.append(WHATSAPP_SERVER_PATH)

try:
    import whatsapp
except ImportError:
    print(f"Could not import whatsapp from {WHATSAPP_SERVER_PATH}. Please ensure it exists.")
    sys.exit(1)


class ChatsLoaded(Message):
    def __init__(self, chats: List[whatsapp.Chat]) -> None:
        self.chats = chats
        super().__init__()

class MessagesLoaded(Message):
    def __init__(self, chat_jid: str, messages: List[whatsapp.Message]) -> None:
        self.chat_jid = chat_jid
        self.messages = messages
        super().__init__()


class ChatListItem(ListItem):
    """A list item representing a single WhatsApp chat."""

    def __init__(self, chat: whatsapp.Chat, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.chat = chat

    def compose(self) -> ComposeResult:
        display_name = self.chat.name or self.chat.jid.split('@')[0]

        time_str = ""
        if self.chat.last_message_time:
            time_str = self.chat.last_message_time.strftime("%H:%M")

        msg_snippet = ""
        if self.chat.last_message:
            msg_snippet = self.chat.last_message[:30] + ("..." if len(self.chat.last_message) > 30 else "")
            if self.chat.last_is_from_me:
                msg_snippet = f"✓ {msg_snippet}"
            elif self.chat.last_sender and self.chat.is_group:
                sender = whatsapp.get_sender_name(self.chat.last_sender)
                msg_snippet = f"{sender}: {msg_snippet}"

        yield Vertical(
            Horizontal(
                Label(display_name, classes="chat-name"),
                Label(time_str, classes="chat-time"),
                classes="chat-item-header"
            ),
            Label(msg_snippet, classes="chat-snippet")
        )

class MessageBubble(Container):
    """A chat bubble for a single message."""

    def __init__(self, msg: whatsapp.Message, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.msg = msg
        if msg.is_from_me:
            self.add_class("-sent")
        else:
            self.add_class("-received")

    def compose(self) -> ComposeResult:
        time_str = self.msg.timestamp.strftime("%H:%M")

        media_tag = ""
        if self.msg.media_type:
            media_tag = f"[{self.msg.media_type.upper()}] ID:{self.msg.id}\n"

        content = self.msg.content if self.msg.content else ""
        text = f"{media_tag}{content}"

        with Vertical(classes="bubble-content"):
            if not self.msg.is_from_me and self.msg.chat_jid.endswith("@g.us"):
                sender_name = whatsapp.get_sender_name(self.msg.sender)
                yield Label(f"{sender_name}", classes="msg-sender")

            yield Label(text)
            yield Label(time_str, classes="msg-time")


class WhatsAppApp(App):
    """A Textual client for WhatsApp."""

    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh Chats")
    ]

    current_chat_jid = reactive(None)

    def __init__(self):
        super().__init__()
        self.last_sync_time = datetime.now()
        # Timer for polling
        self.poll_timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-container"):
            with Vertical(id="sidebar"):
                with Vertical(id="sidebar-header"):
                    yield Label("Chats", id="sidebar-title")
                    yield Input(placeholder="Search chats...", id="search-input")
                yield ListView(id="chat-list")

            with Vertical(id="chat-area"):
                with ScrollableContainer(id="message-container"):
                    pass
                with Container(id="input-area"):
                    yield Input(placeholder="Type a msg or command (e.g. /file, /download)...", id="message-input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "WhatsApp TUI"
        self.load_chats()
        # Start polling for new messages every 3 seconds
        self.poll_timer = self.set_interval(3.0, self.check_new_messages)

    @work(exclusive=True, thread=True)
    def load_chats(self, query: str = None) -> None:
        try:
            chats = whatsapp.list_chats(limit=50, query=query)
            self.post_message(ChatsLoaded(chats))
        except Exception as e:
            self.log(f"Error loading chats: {e}")

    def on_chats_loaded(self, event: ChatsLoaded) -> None:
        chat_list = self.query_one("#chat-list", ListView)
        chat_list.clear()

        for chat in event.chats:
            chat_list.append(ChatListItem(chat))

    @work(exclusive=True, thread=True)
    def load_messages(self, chat_jid: str) -> None:
        try:
            conn = sqlite3.connect(whatsapp.MESSAGES_DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT messages.timestamp, messages.sender, chats.name, messages.content,
                       messages.is_from_me, chats.jid, messages.id, messages.media_type
                FROM messages
                JOIN chats ON messages.chat_jid = chats.jid
                WHERE messages.chat_jid = ?
                ORDER BY messages.timestamp DESC
                LIMIT 50
            """, (chat_jid,))

            raw_messages = cursor.fetchall()

            messages = []
            for msg in raw_messages:
                message = whatsapp.Message(
                    timestamp=datetime.fromisoformat(msg[0]),
                    sender=msg[1],
                    chat_name=msg[2],
                    content=msg[3],
                    is_from_me=msg[4],
                    chat_jid=msg[5],
                    id=msg[6],
                    media_type=msg[7]
                )
                messages.append(message)

            messages.reverse()
            self.post_message(MessagesLoaded(chat_jid, messages))
        except Exception as e:
            self.log(f"Error loading messages: {e}")

    async def on_messages_loaded(self, event: MessagesLoaded) -> None:
        if event.chat_jid != self.current_chat_jid:
            return

        container = self.query_one("#message-container", ScrollableContainer)
        container.query("*").remove() # clear existing messages

        if not event.messages:
            await container.mount(Label("No messages yet.", classes="system-msg"))
            return

        bubbles = [MessageBubble(msg) for msg in event.messages]
        await container.mount_all(bubbles)

        # Scroll to bottom after the UI has updated
        self.call_after_refresh(container.scroll_end, animate=False)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, ChatListItem):
            self.current_chat_jid = item.chat.jid

            container = self.query_one("#message-container", ScrollableContainer)
            container.query("*").remove()
            container.mount(Label("Loading...", classes="system-msg"))

            self.load_messages(item.chat.jid)
            self.query_one("#message-input").focus()

    @work(thread=True)
    def check_new_messages(self) -> None:
        """Poll the database for messages newer than last_sync_time."""
        try:
            conn = sqlite3.connect(whatsapp.MESSAGES_DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT messages.timestamp, messages.sender, chats.name, messages.content,
                       messages.is_from_me, chats.jid, messages.id, messages.media_type
                FROM messages
                JOIN chats ON messages.chat_jid = chats.jid
                WHERE messages.timestamp > ?
                ORDER BY messages.timestamp ASC
            """, (self.last_sync_time.isoformat(),))

            new_msgs = cursor.fetchall()
            if not new_msgs:
                return

            should_refresh_chat = False
            should_refresh_list = len(new_msgs) > 0

            for msg_data in new_msgs:
                msg_time = datetime.fromisoformat(msg_data[0])
                if msg_time > self.last_sync_time:
                    self.last_sync_time = msg_time

                is_from_me = msg_data[4]
                chat_jid = msg_data[5]
                sender = msg_data[1]
                chat_name = msg_data[2] or chat_jid.split('@')[0]

                # Show notification for received messages
                if not is_from_me:
                    sender_name = whatsapp.get_sender_name(sender)
                    title = f"Message from {sender_name}" if sender_name != chat_name else chat_name
                    content = msg_data[3] or f"[{msg_data[7]}]"
                    self.call_from_thread(self.notify, content[:50], title=title)

                if chat_jid == self.current_chat_jid:
                    should_refresh_chat = True

            if should_refresh_chat and self.current_chat_jid:
                self.load_messages(self.current_chat_jid)

            if should_refresh_list:
                search_query = self.query_one("#search-input").value
                self.load_chats(query=search_query if search_query else None)

        except Exception as e:
            self.log(f"Error polling messages: {e}")

    # --- Search ---

    # Debounce timer for search
    search_timer = None

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle typing in inputs (specifically search)."""
        if event.input.id == "search-input":
            if self.search_timer:
                self.search_timer.stop()
            # Wait 0.5s after typing stops before searching
            self.search_timer = self.set_timer(0.5, self._execute_search)

    def _execute_search(self) -> None:
        query = self.query_one("#search-input").value.strip()
        self.load_chats(query=query if query else None)


    # --- Message & Commands ---

    @work(exclusive=True, thread=True)
    def send_whatsapp_message(self, recipient: str, text: str) -> None:
        try:
            success, status = whatsapp.send_message(recipient, text)
            if success:
                self.load_messages(recipient)
                self.load_chats()
            else:
                self.call_from_thread(self.notify, f"Failed to send: {status}", title="Error", severity="error")
        except Exception as e:
            self.call_from_thread(self.notify, f"Error: {e}", title="Error", severity="error")

    @work(exclusive=True, thread=True)
    def send_whatsapp_file(self, recipient: str, path: str) -> None:
        try:
            success, status = whatsapp.send_file(recipient, path)
            if success:
                self.call_from_thread(self.notify, f"File sent: {path}")
                time.sleep(1) # Give db time to update
                self.load_messages(recipient)
            else:
                self.call_from_thread(self.notify, f"Failed to send file: {status}", title="Error", severity="error")
        except Exception as e:
            self.call_from_thread(self.notify, f"Error: {e}", title="Error", severity="error")

    @work(exclusive=True, thread=True)
    def download_whatsapp_media(self, msg_id: str, chat_jid: str) -> None:
        try:
            path = whatsapp.download_media(msg_id, chat_jid)
            if path:
                self.call_from_thread(self.notify, f"Downloaded to: {path}", title="Success")
            else:
                self.call_from_thread(self.notify, "Failed to download media", title="Error", severity="error")
        except Exception as e:
            self.call_from_thread(self.notify, f"Error: {e}", title="Error", severity="error")


    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "message-input":
            return

        if not self.current_chat_jid:
            return

        text = event.value.strip()
        if not text:
            return

        event.input.value = ""

        # Check for commands
        if text.startswith("/file "):
            path = text[6:].strip()
            self.notify(f"Sending file {path}...")
            self.send_whatsapp_file(self.current_chat_jid, path)
        elif text.startswith("/download "):
            msg_id = text[10:].strip()
            self.notify(f"Downloading media {msg_id}...")
            self.download_whatsapp_media(msg_id, self.current_chat_jid)
        elif text.startswith("/"):
            self.notify(f"Unknown command. Try /file <path> or /download <msg_id>", title="Error", severity="warning")
        else:
            self.send_whatsapp_message(self.current_chat_jid, text)

    def action_refresh(self) -> None:
        search_query = self.query_one("#search-input").value
        self.load_chats(query=search_query if search_query else None)
        if self.current_chat_jid:
            self.load_messages(self.current_chat_jid)


if __name__ == "__main__":
    app = WhatsAppApp()
    app.run()
