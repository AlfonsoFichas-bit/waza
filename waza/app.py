import sys
import os
import sqlite3
from typing import Optional, List
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, ListView, ListItem, Label, Input, RichLog
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

# We define some custom Messages to tell the app when data is loaded
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
        # Show chat name or JID if name is empty
        display_name = self.chat.name or self.chat.jid.split('@')[0]

        # Format last message time if it exists
        time_str = ""
        if self.chat.last_message_time:
            time_str = self.chat.last_message_time.strftime("%H:%M %d/%m")

        # Truncate last message snippet
        msg_snippet = ""
        if self.chat.last_message:
            msg_snippet = self.chat.last_message[:30] + ("..." if len(self.chat.last_message) > 30 else "")
            if self.chat.last_is_from_me:
                msg_snippet = f"You: {msg_snippet}"
            elif self.chat.last_sender and self.chat.is_group:
                # Group message from someone else
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


class WhatsAppApp(App):
    """A Textual client for WhatsApp."""

    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh Chats")
    ]

    current_chat_jid = reactive(None)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-container"):
            # Sidebar for chats
            with Vertical(id="sidebar"):
                yield Label("Chats", id="sidebar-title")
                yield ListView(id="chat-list")

            # Main window for messages
            with Vertical(id="chat-area"):
                yield RichLog(id="message-log", wrap=True, highlight=True, markup=True)
                yield Input(placeholder="Type a message... (Enter to send)", id="message-input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "WhatsApp TUI"
        self.load_chats()

    @work(exclusive=True, thread=True)
    def load_chats(self) -> None:
        """Fetch chats in a background thread."""
        try:
            chats = whatsapp.list_chats(limit=50)
            # Post message to the app to update UI in main thread
            self.post_message(ChatsLoaded(chats))
        except Exception as e:
            self.log(f"Error loading chats: {e}")

    def on_chats_loaded(self, event: ChatsLoaded) -> None:
        """Update the UI with the loaded chats."""
        chat_list = self.query_one("#chat-list", ListView)
        chat_list.clear()

        for chat in event.chats:
            chat_list.append(ChatListItem(chat))

    @work(exclusive=True, thread=True)
    def load_messages(self, chat_jid: str) -> None:
        """Fetch messages for a chat in a background thread."""
        try:
            # We want to get the raw message objects, not the formatted string output
            # By default list_messages returns formatted strings if include_context is true or false but returns
            # To get raw messages, we need to bypass the format_messages_list call in whatsapp.py
            # But since we can't easily modify whatsapp.py without breaking the mcp server potentially,
            # we'll use a direct SQLite query similar to whatsapp.list_messages but returning objects.
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

            # Sort messages chronologically for display
            messages.reverse()
            self.post_message(MessagesLoaded(chat_jid, messages))
        except Exception as e:
            self.log(f"Error loading messages: {e}")

    def on_messages_loaded(self, event: MessagesLoaded) -> None:
        """Update the UI with loaded messages."""
        if event.chat_jid != self.current_chat_jid:
            return  # The user selected a different chat while this was loading

        log = self.query_one("#message-log", RichLog)
        log.clear()

        if not event.messages:
            log.write("[italic]No messages yet.[/italic]")
            return

        for msg in event.messages:
            self.display_message(msg, log)

    def display_message(self, msg: whatsapp.Message, log: RichLog) -> None:
        """Format and display a single message in the RichLog."""
        time_str = msg.timestamp.strftime("%H:%M")

        media_tag = ""
        if msg.media_type:
            media_tag = f"[[bold blue]{msg.media_type}[/]] "

        content = msg.content if msg.content else ""

        if msg.is_from_me:
            log.write(f"[dim]{time_str}[/dim] [bold green]You[/]: {media_tag}{content}")
        else:
            sender_name = whatsapp.get_sender_name(msg.sender)
            log.write(f"[dim]{time_str}[/dim] [bold blue]{sender_name}[/]: {media_tag}{content}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Called when a chat is selected."""
        item = event.item
        if isinstance(item, ChatListItem):
            self.current_chat_jid = item.chat.jid

            # Clear log and show loading indicator
            log = self.query_one("#message-log", RichLog)
            log.clear()
            log.write("[italic]Loading messages...[/italic]")

            # Load messages for the selected chat
            self.load_messages(item.chat.jid)

            # Focus the input
            self.query_one("#message-input").focus()

    @work(exclusive=True, thread=True)
    def send_whatsapp_message(self, recipient: str, text: str) -> None:
        """Send message in a background thread."""
        try:
            success, status = whatsapp.send_message(recipient, text)
            if success:
                # Reload messages to show the newly sent one
                self.load_messages(recipient)
            else:
                # Inform the user of failure
                self.call_from_thread(self.show_error, f"Failed to send: {status}")
        except Exception as e:
            self.call_from_thread(self.show_error, f"Error sending message: {e}")

    def show_error(self, message: str) -> None:
        log = self.query_one("#message-log", RichLog)
        log.write(f"[bold red]{message}[/]")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Called when the user presses Enter in the input box."""
        if not self.current_chat_jid:
            return

        text = event.value.strip()
        if not text:
            return

        # Send the message
        self.send_whatsapp_message(self.current_chat_jid, text)

        # Clear the input
        event.input.value = ""

    def action_refresh(self) -> None:
        """Action for 'r' key binding."""
        self.load_chats()
        if self.current_chat_jid:
            self.load_messages(self.current_chat_jid)


if __name__ == "__main__":
    app = WhatsAppApp()
    app.run()
