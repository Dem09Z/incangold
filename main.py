import os
import random
import asyncio
from pathlib import Path
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load Environment Variables
env_path = Path(__file__).resolve().parent / "token.env"
load_dotenv(dotenv_path=env_path)
TOKEN = os.getenv("DISCORD_TOKEN")

# --- Default Decks ---
DEFAULT_TREASURES = [1, 2, 3, 4, 5, 5, 7, 7, 9, 11, 11, 13, 14, 15, 17]
DEFAULT_HAZARDS = {
    "⛰️ Rockfall": 3,
    "🧟 Zombie": 3,
    "🔥 Fire": 3,
    "🕷️ Spider": 3,
    "🪤 Trap": 3
}

# In-memory Deck Database
CUSTOM_DECKS = {
    "default": {
        "treasures": list(DEFAULT_TREASURES),
        "hazards": dict(DEFAULT_HAZARDS)
    }
}

# --- Game Engine ---
class IncanGoldGame:
    def __init__(self, channel_id: int, creator: discord.Member, max_players: int, total_rounds: int, deck_name: str):
        self.channel_id = channel_id
        self.creator = creator
        self.max_players = max_players
        self.total_rounds = total_rounds
        self.deck_name = deck_name
        
        self.players = [creator]
        self.scores = {creator.id: 0}
        self.round_gems = {creator.id: 0}
        self.status = {creator.id: "exploring"}
        
        self.current_round = 0
        self.deck = []
        self.path_cards = []
        self.drawn_hazards = set()
        self.artifact_pool = self._generate_artifact_pool(total_rounds)
        
        self.is_started = False
        self.countdown_task = None

    def _generate_artifact_pool(self, rounds: int):
        pool = []
        extra_artifacts_count = rounds - 1
        num_five = (extra_artifacts_count + 1) // 2
        num_ten = extra_artifacts_count // 2
        
        for _ in range(num_five):
            pool.append({"type": "artifact", "name": "🗿 Artifact", "value": 5})
        for _ in range(num_ten):
            pool.append({"type": "artifact", "name": "🗿 Artifact", "value": 10})
        return pool

    def setup_deck_for_round(self):
        deck_data = CUSTOM_DECKS.get(self.deck_name, CUSTOM_DECKS["default"])
        
        self.deck = []
        for val in deck_data["treasures"]:
            self.deck.append({"type": "treasure", "name": f"💎 Treasure {val}", "value": val, "remainder": 0})
            
        for h_name, count in deck_data["hazards"].items():
            for _ in range(count):
                self.deck.append({"type": "hazard", "name": h_name})
                
        if self.current_round == 1:
            self.deck.append({"type": "artifact", "name": "🗿 Artifact", "value": 5})
        elif self.artifact_pool:
            self.deck.append(self.artifact_pool.pop(0))
            
        random.shuffle(self.deck)


ACTIVE_GAMES = {}


# --- UI Views ---

class LobbyView(discord.ui.View):
    def __init__(self, game: IncanGoldGame):
        super().__init__(timeout=300)
        self.game = game

    @discord.ui.button(label="Join Game ✋", style=discord.ButtonStyle.primary, custom_id="btn_join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user in self.game.players:
            return await interaction.response.send_message("You are already in this game!", ephemeral=True)
        if len(self.game.players) >= self.game.max_players:
            return await interaction.response.send_message("The game lobby is full!", ephemeral=True)
            
        self.game.players.append(interaction.user)
        self.game.scores[interaction.user.id] = 0
        self.game.round_gems[interaction.user.id] = 0
        self.game.status[interaction.user.id] = "exploring"
        
        await interaction.response.send_message(f"🎮 {interaction.user.mention} joined the game! ({len(self.game.players)}/{self.game.max_players})")
        
        if len(self.game.players) == self.game.max_players and not self.game.countdown_task:
            self.game.countdown_task = asyncio.create_task(self.start_countdown(interaction.channel))

    @discord.ui.button(label="Start Game 🚀", style=discord.ButtonStyle.green, custom_id="btn_start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.game.creator:
            return await interaction.response.send_message("Only the game creator can force start.", ephemeral=True)
        
        if self.game.countdown_task:
            self.game.countdown_task.cancel()
            
        await interaction.response.send_message("🚀 Starting Incan Gold!")
        self.stop()
        await start_next_round(interaction.channel, self.game)

    async def start_countdown(self, channel):
        await channel.send("⏳ Lobby is full! Starting game in **10 seconds**...")
        await asyncio.sleep(10)
        self.stop()
        await start_next_round(channel, self.game)


class ChoiceView(discord.ui.View):
    def __init__(self, game: IncanGoldGame, active_players: list):
        super().__init__(timeout=None)
        self.game = game
        self.active_players = active_players
        self.choices = {}

    async def check_all_decided(self):
        if len(self.choices) == len(self.active_players):
            self.stop()

    @discord.ui.button(label="Go Deeper 🤠", style=discord.ButtonStyle.green, custom_id="btn_continue")
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.active_players:
            return await interaction.response.send_message("You are not active in this round or already in camp!", ephemeral=True)
        
        self.choices[interaction.user.id] = "continue"
        await interaction.response.send_message("🤠 You chose to **CONTINUE**! (Waiting for others...)", ephemeral=True)
        await self.check_all_decided()

    @discord.ui.button(label="Return to Camp 🏃⛺", style=discord.ButtonStyle.danger, custom_id="btn_goback")
    async def goback_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.active_players:
            return await interaction.response.send_message("You are not active in this round or already in camp!", ephemeral=True)
        
        self.choices[interaction.user.id] = "goback"
        await interaction.response.send_message("🏃 You chose to **RETURN TO CAMP**! (Waiting for others...)", ephemeral=True)
        await self.check_all_decided()


# --- Game Engine Routines ---

def create_board_embed(game: IncanGoldGame, waiting_for_players: list = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏛️ Incan Gold - Round {game.current_round}/{game.total_rounds}",
        color=discord.Color.gold()
    )
    
    path_str = ""
    if not game.path_cards:
        path_str = "*No cards on the path yet*"
    else:
        cards_display = []
        for c in game.path_cards:
            if c["type"] == "treasure":
                rem = f" [Remainder: {c['remainder']}]" if c['remainder'] > 0 else ""
                cards_display.append(f"`{c['name']}{rem}`")
            elif c["type"] == "artifact":
                rem = " [On Path]" if c.get("on_path", True) else " [Claimed]"
                cards_display.append(f"`{c['name']} ({c['value']}pt){rem}`")
            else:
                cards_display.append(f"`{c['name']}`")
        path_str = " ➔ ".join(cards_display)
        
    embed.add_field(name="📜 Path Cards (Left to Right)", value=path_str, inline=False)
    
    player_status_list = []
    exploring_count = 0
    for p in game.players:
        st = game.status[p.id]
        score = game.scores[p.id]
        r_gems = game.round_gems[p.id]
        if st == "exploring":
            exploring_count += 1
            player_status_list.append(f"🤠 **{p.display_name}**: In Temple (Unbanked: 💎 {r_gems})")
        else:
            player_status_list.append(f"⛺ **{p.display_name}**: At Camp (Safe Score: 💎 {score})")
            
    embed.add_field(name="👥 Player Status", value="\n".join(player_status_list), inline=False)
    embed.add_field(name="📊 Players Continuing", value=f"**{exploring_count}** player(s)", inline=True)
    
    if waiting_for_players:
        pending_names = ", ".join(p.display_name for p in waiting_for_players)
        embed.add_field(name="⏳ Waiting for decision", value=f"Waiting for: **{pending_names}**", inline=False)
        
    return embed


async def start_next_round(channel, game: IncanGoldGame):
    game.current_round += 1
    if game.current_round > game.total_rounds:
        return await finish_game(channel, game)
        
    game.path_cards = []
    game.drawn_hazards = set()
    for p in game.players:
        game.status[p.id] = "exploring"
        game.round_gems[p.id] = 0
        
    game.setup_deck_for_round()
    
    await channel.send(f"\n🔔 **Starting Round {game.current_round}!** Entering the temple...")
    await asyncio.sleep(2)
    await run_turn(channel, game)


async def run_turn(channel, game: IncanGoldGame):
    if game.channel_id not in ACTIVE_GAMES:
        return

    exploring_players = [p for p in game.players if game.status[p.id] == "exploring"]
    
    if not exploring_players:
        await channel.send("⛺ All players have returned to camp! Ending round.")
        await asyncio.sleep(2)
        return await start_next_round(channel, game)

    suspense_msg = await channel.send("🔍 *Flipping the next card...*")
    await asyncio.sleep(2.5)

    card = game.deck.pop(0)
    game.path_cards.append(card)
    
    await suspense_msg.edit(content=f"🃏 **Drawn Card:** `{card['name']}`!")
    await asyncio.sleep(1.5)
    
    if card["type"] == "treasure":
        val = card["value"]
        share = val // len(exploring_players)
        remainder = val % len(exploring_players)
        
        card["remainder"] = remainder
        for p in exploring_players:
            game.round_gems[p.id] += share
            
        await channel.send(f"💎 Distributed **{share}** gems each! (Left on card: **{remainder})")
        await asyncio.sleep(1.5)

    elif card["type"] == "artifact":
        card["on_path"] = True
        await channel.send(f"🗿 Revealed  ({card['value']} pts)**! (Claimable only if you return alone)")
        await asyncio.sleep(1.5)

    elif card["type"] == "hazard":
        h_name = card["name"]
        if h_name in game.drawn_hazards:
            embed = create_board_embed(game)
            await channel.send(embed=embed)
            await channel.send(f"💥 **PARTY BUSTED!! Second duplicate hazard ({h_name})!**\nAll remaining explorers lost their unbanked gems!")
            
            for p in exploring_players:
                game.round_gems[p.id] = 0
                
            deck_data = CUSTOM_DECKS.get(game.deck_name, CUSTOM_DECKS["default"])
            if deck_data["hazards"].get(h_name, 0) > 1:
                deck_data["hazards"][h_name] -= 1
                await channel.send(f"⚠️ `{h_name}` has been permanently removed from the deck (1 copy removed).")
                
            await asyncio.sleep(3)
            return await start_next_round(channel, game)
        else:
            game.drawn_hazards.add(h_name)
            await channel.send(f"⚠️ Danger warning! First hazard ({h_name}). Stay cautious...")
            await asyncio.sleep(1.5)

    embed = create_board_embed(game, waiting_for_players=exploring_players)
    view = ChoiceView(game, exploring_players)
    await channel.send(embed=embed, view=view)
    
    await view.wait()

    if game.channel_id not in ACTIVE_GAMES:
        return
    
    leavers = [p for p in exploring_players if view.choices.get(p.id) == "goback"]
    
    if leavers:
        await channel.send(f"🚪 Decision Revealed! **{len(leavers)}** player(s) decided to return to camp!")
        await asyncio.sleep(1.5)
        
        total_remainders = sum(c["remainder"] for c in game.path_cards if c["type"] == "treasure" and c["remainder"] > 0)
        if total_remainders > 0:
            share_rem = total_remainders // len(leavers)
            leftover_rem = total_remainders % len(leavers)
            
            for p in leavers:
                game.round_gems[p.id] += share_rem
                
            for c in game.path_cards:
                if c["type"] == "treasure":
                    c["remainder"] = 0
            
            if game.path_cards:
                left_over_card = next((c for c in game.path_cards if c["type"] == "treasure"), None)
                if left_over_card:
                    left_over_card["remainder"] = leftover_rem
                
            await channel.send(f"🏃 Players returning ({', '.join(p.display_name for p in leavers)}) split path leftovers: **{share_rem}** gems each!")

        if len(leavers) == 1:
            solo_leaver = leavers[0]
            for c in game.path_cards:
                if c["type"] == "artifact" and c.get("on_path", False):
                    c["on_path"] = False
                    game.scores[solo_leaver.id] += c["value"]
                    await channel.send(f"🗿 {solo_leaver.mention} claimed **{c['name']} ({c['value']} pts)** by returning alone!")

        for p in leavers:
            game.scores[p.id] += game.round_gems[p.id]
            game.round_gems[p.id] = 0
            game.status[p.id] = "tent"
    else:
        await channel.send("🤠 All active players decided to **GO DEEPER**!")

    await asyncio.sleep(2)
    await run_turn(channel, game)


async def finish_game(channel, game: IncanGoldGame):
    embed = discord.Embed(
        title="🏆 Incan Gold - Final Scoreboard!",
        color=discord.Color.gold()
    )
    
    sorted_players = sorted(game.players, key=lambda p: game.scores[p.id], reverse=True)
    leaderboard = []
    
    for i, p in enumerate(sorted_players, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🎖️"
        leaderboard.append(f"{medal} **#{i} {p.display_name}**: {game.scores[p.id]} points")
        
    embed.add_field(name="🎉 Standings", value="\n".join(leaderboard), inline=False)
    await channel.send(embed=embed)
    
    if channel.id in ACTIVE_GAMES:
        del ACTIVE_GAMES[channel.id]


# --- Bot Commands ---

class IncanGoldBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("Global Slash Commands synced!")

bot = IncanGoldBot()

# Helper command to remove duplicate guild-specific commands
@bot.command(name="clearguild")
async def clear_guild_commands(ctx):
    bot.tree.clear_commands(guild=ctx.guild)
    await bot.tree.sync(guild=ctx.guild)
    await ctx.send("🧹 Cleared guild duplicate commands! Press `Ctrl+R` to refresh Discord.")


@bot.tree.command(name="create", description="Create an Incan Gold game lobby")
@app_commands.describe(
    max_player_count="Maximum allowed players",
    game_round="Total number of rounds",
    card_deck="Deck template to use"
)
async def create_game(interaction: discord.Interaction, max_player_count: int, game_round: int, card_deck: str = "default"):
    if interaction.channel_id in ACTIVE_GAMES:
        return await interaction.response.send_message("A game is already running in this channel!", ephemeral=True)
        
    if card_deck not in CUSTOM_DECKS:
        return await interaction.response.send_message(f"Deck `{card_deck}` not found. (Use 'default' or check `/deck list`)", ephemeral=True)

    game = IncanGoldGame(interaction.channel_id, interaction.user, max_player_count, game_round, card_deck)
    ACTIVE_GAMES[interaction.channel_id] = game
    
    embed = discord.Embed(
        title="🏛️ Incan Gold Lobby Created!",
        description=f"**Host:** {interaction.user.mention}\n**Max Players:** {max_player_count}\n**Rounds:** {game_round}\n**Deck:** {card_deck}",
        color=discord.Color.green()
    )
    view = LobbyView(game)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="terminate", description="Cancel and end the ongoing game in this channel")
async def terminate_game(interaction: discord.Interaction):
    if interaction.channel_id not in ACTIVE_GAMES:
        return await interaction.response.send_message("There is no ongoing game running in this channel.", ephemeral=True)
        
    game = ACTIVE_GAMES[interaction.channel_id]
    if game.countdown_task and not game.countdown_task.done():
        game.countdown_task.cancel()
        
    del ACTIVE_GAMES[interaction.channel_id]
    await interaction.response.send_message("🛑 **Game Terminated!** The active game session has been cancelled.")


@bot.tree.command(name="rules", description="View Incan Gold game rules and mechanics")
async def game_rules(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 Incan Gold - Game Rules & How to Play",
        description="Delve into ancient temple ruins to collect valuable gems, but retreat to camp before traps and disasters strike!",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="🤠 Core Choice",
        value="• Each turn, all active explorers simultaneously choose to **Go Deeper** 🤠 or **Return to Camp** 🏃⛺.\n"
              "• **Go Deeper**: Flip a new card from the deck.\n"
              "• **Return to Camp**: Safely store all unbanked gems collected in the current round into your chest.",
        inline=False
    )
    embed.add_field(
        name="🃏 Card Types",
        value="• 💎 **Treasures**: Gems on the card are split evenly among all active explorers. Remaining gems stay on the path card.\n"
              "• 🗿 **Artifacts**: High-value bonus points! Can only be claimed if **EXACTLY ONE** player returns to camp on that turn.\n"
              "• ⚠️ **Hazards**: 5 types (Rockfall, Zombie, Fire, Spider, Trap). 3 copies of each in the default deck.",
        inline=False
    )
    embed.add_field(
        name="💥 Party Busting & Hazard Loss",
        value="• Drawing the **1st copy** of a hazard is a warning.\n"
              "• Drawing a **2nd duplicate copy** of the same hazard causes a **PARTY BUST**!\n"
              "• Busted explorers lose ALL unbanked gems for that round.\n"
              "• 1 copy of that duplicate hazard is permanently removed from the deck.",
        inline=False
    )
    embed.add_field(
        name="🏃 Leaving Leftovers",
        value="• Players returning to camp split all leftover gems left behind on treasure cards on their way back.",
        inline=False
    )
    await interaction.response.send_message(embed=embed)


# --- Deck Management Commands ---

deck_group = app_commands.Group(name="deck", description="Manage custom card decks")

@deck_group.command(name="create", description="Create a custom deck template")
async def deck_create(interaction: discord.Interaction, name: str):
    if name.lower() == "default":
        return await interaction.response.send_message("Cannot override 'default' deck.", ephemeral=True)
    if name in CUSTOM_DECKS:
        return await interaction.response.send_message("A deck with this name already exists.", ephemeral=True)
        
    CUSTOM_DECKS[name] = {
        "treasures": list(DEFAULT_TREASURES),
        "hazards": dict(DEFAULT_HAZARDS)
    }
    await interaction.response.send_message(f"✅ Created Deck `{name}` (Copied from default template).")

@deck_group.command(name="delete", description="Delete a custom deck")
async def deck_delete(interaction: discord.Interaction, name: str):
    if name.lower() == "default":
        return await interaction.response.send_message("❌ Cannot delete default deck!", ephemeral=True)
    if name not in CUSTOM_DECKS:
        return await interaction.response.send_message("Deck not found.", ephemeral=True)
        
    del CUSTOM_DECKS[name]
    await interaction.response.send_message(f"🗑️ Deleted deck `{name}` successfully.")

@deck_group.command(name="list", description="List all available custom decks")
async def deck_list(interaction: discord.Interaction):
    embed = discord.Embed(title="🃏 Available Card Decks", color=discord.Color.blue())
    for d_name, d_data in CUSTOM_DECKS.items():
        treasures_str = ", ".join(map(str, sorted(d_data["treasures"])))
        hazards_str = ", ".join([f"{k}: {v}" for k, v in d_data["hazards"].items()])
        embed.add_field(
            name=f"📦 Deck: {d_name}",
            value=f"💎 **Treasures:** [{treasures_str}]\n⚠️ **Hazards:** {hazards_str}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)

@deck_group.command(name="edit_hazard", description="Change hazard card counts in a custom deck")
async def deck_edit_hazard(interaction: discord.Interaction, name: str, hazard_name: str, count: int):
    if name.lower() == "default":
        return await interaction.response.send_message("Cannot modify 'default' deck.", ephemeral=True)
    if name not in CUSTOM_DECKS:
        return await interaction.response.send_message(f"Deck `{name}` not found.", ephemeral=True)
    if count < 0:
        return await interaction.response.send_message("Count cannot be negative.", ephemeral=True)
        
    CUSTOM_DECKS[name]["hazards"][hazard_name] = count
    await interaction.response.send_message(f"✏️ Deck `{name}` updated: Hazard `{hazard_name}` set to **{count}** copies.")

@deck_group.command(name="add_treasure", description="Add a treasure card to a custom deck")
async def deck_add_treasure(interaction: discord.Interaction, name: str, value: int):
    if name.lower() == "default":
        return await interaction.response.send_message("Cannot modify 'default' deck.", ephemeral=True)
    if name not in CUSTOM_DECKS:
        return await interaction.response.send_message(f"Deck `{name}` not found.", ephemeral=True)
        
    CUSTOM_DECKS[name]["treasures"].append(value)
    await interaction.response.send_message(f"💎 Added Treasure card with value **{value}** to deck `{name}`.")

@deck_group.command(name="remove_treasure", description="Remove a treasure card from a custom deck")
async def deck_remove_treasure(interaction: discord.Interaction, name: str, value: int):
    if name.lower() == "default":
        return await interaction.response.send_message("Cannot modify 'default' deck.", ephemeral=True)
    if name not in CUSTOM_DECKS:
        return await interaction.response.send_message(f"Deck `{name}` not found.", ephemeral=True)
    if value not in CUSTOM_DECKS[name]["treasures"]:
        return await interaction.response.send_message(f"No treasure with value **{value}** found in deck `{name}`.", ephemeral=True)
        
    CUSTOM_DECKS[name]["treasures"].remove(value)
    await interaction.response.send_message(f"🗑️ Removed one Treasure card with value **{value}** from deck `{name}`.")

bot.tree.add_command(deck_group)

if __name__ == "__main__":
    bot.run(TOKEN)