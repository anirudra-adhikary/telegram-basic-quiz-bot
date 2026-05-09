import logging
import os
import json
import random
import re

from dotenv import load_dotenv
from telegram import Update, Poll
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut, BadRequest
from telegram.request import HTTPXRequest  # Import HTTPXRequest for timeout config
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
    CallbackContext,
    PicklePersistence
)

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()  # Load environment variables from .env file

import json
from telegram.ext import Application, CommandHandler

QUIZ_DATABASES = {
    "java": "questions_java.json",
    "python": "questions_python.json",
    "competitive": "questions_competitive.json",
    "jeca": "questions_jeca.json",
    "demo": "questions_jeca.json",
}

# --- Bot Handlers ---

async def bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a new quiz in the group chat, only if initiated by an admin."""
    chat = update.effective_chat
    user = update.effective_user

    if not user or not chat:
        logger.warning("Could not get user or chat from update.")
        return
        
    # --- Admin Check Logic ---
    if chat.type == 'private':
        await update.message.reply_text("This command only works in groups! 😅")
        return

    try:
        chat_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
    except Exception as e:
        logger.error(f"Error checking chat member status: {e}")
        await update.message.reply_text("I couldn't check your permissions. 😬\nMake sure I have admin rights to see other members.")
        return

    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("Sorry, only group admins can start a quiz! ⛔️")
        return
        
    logger.info(f"Admin check passed for user {user.id} in chat {chat.id}. Starting quiz...")
    # --- End Admin Check Logic ---

    chat_id = update.effective_chat.id
    active_quiz_key = f'active_quiz_{chat_id}'
    
    if context.bot_data.get(active_quiz_key, {}).get('quiz_active', False):
        await update.message.reply_text(
            "A quiz is already in progress in this group! 😮\n"
            "Wait for it to finish before starting a new one."
        )
        return
    
    command = update.message.text.split()[0][1:]
    database = QUIZ_DATABASES.get(command)

    if not database:
        await update.message.reply_text("Invalid command")
        return
    
    try:
        with open(database, 'r', encoding="utf-8") as f:
            questions_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Error loading questions: {e}")
        await update.message.reply_text("❌ Sorry, I couldn't load the quiz questions right now.")
        return

    all_questions = questions_data.get("questions", [])
    QUIZ_LENGTH = 3
    
    if len(all_questions) < QUIZ_LENGTH:
        await update.message.reply_text(f"❌ Not enough questions in the database. Need at least {QUIZ_LENGTH}.")
        return

    selected_questions = random.sample(all_questions, QUIZ_LENGTH)
    
    quiz_data = {
        'quiz_questions': selected_questions,
        'current_question_index': 0,
        'scores': {},
        'quiz_active': True,
        'chat_id': chat_id
    }
    
    context.bot_data[active_quiz_key] = quiz_data

    await update.message.reply_text(
        f"🎯 **Quiz Started!**\n\n"
        f"• {QUIZ_LENGTH} random questions\n"
        "• 45 seconds per question\n\n"
        "Good luck! 🍀",
        parse_mode='Markdown'
    )

    await send_next_question(context, quiz_data)

async def send_next_question(context: CallbackContext, quiz_data=None) -> None:
    """Sends the next question (and code snippet, if any) or ends the quiz."""
    
    if quiz_data is None:
        logger.warning("send_next_question called without quiz_data")
        return
        
    chat_id = quiz_data.get('chat_id')
    current_index = quiz_data.get('current_question_index', 0)
    questions = quiz_data.get('quiz_questions', [])
    active_quiz_key = f'active_quiz_{chat_id}'
    
    if current_index >= len(questions):
        logger.info(f"Quiz completed! Ending quiz for chat {chat_id}")
        await end_quiz(context, quiz_data)
        return

    question_data = questions[current_index]
    db_id = question_data.get("id", "Unknown ID") 
    quiz_length = len(questions)
    
    # --- 1. PRE-VALIDATION ---
    # Telegram Limit: Poll options must be <= 100 chars
    options = question_data["options"]
    for i, option in enumerate(options):
        if len(option) > 100:
            logger.error(f"⚠️ Question DB ID {db_id} skipped: Option {i+1} is too long ({len(option)} chars). Max is 100.")
            
            # Skip this question immediately
            quiz_data['current_question_index'] = current_index + 1
            context.bot_data[active_quiz_key] = quiz_data
            
            # Trigger next question immediately
            context.job_queue.run_once(
                lambda ctx: ctx.application.create_task(send_next_question_callback(ctx, chat_id)), 
                1
            )
            return

    # Telegram Limit: Poll question must be <= 300 chars
    TELEGRAM_POLL_QUESTION_LIMIT = 300 
    prefix = f"Question {current_index + 1}/{quiz_length}: "
    full_question_text = f"{prefix}{question_data['question']}"
    
    if len(full_question_text) > TELEGRAM_POLL_QUESTION_LIMIT:
        logger.error(f"⚠️ Question DB ID {db_id} skipped: Question text too long.")
        # Skip logic (same as above)
        quiz_data['current_question_index'] = current_index + 1
        context.bot_data[active_quiz_key] = quiz_data
        context.job_queue.run_once(
            lambda ctx: ctx.application.create_task(send_next_question_callback(ctx, chat_id)), 
            1
        )
        return

    logger.info(f"Sending question {current_index + 1} (DB ID: {db_id})")
    
    try:
        # --- Code Snippet Logic (Unchanged) ---
        if "code_snippet" in question_data and question_data["code_snippet"]:
            code = question_data["code_snippet"]
            lang = question_data.get("language", "")
            formatted_code_text = f"```{lang}\n{code}\n```"
            
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=formatted_code_text,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Code:\n{code}"
                )

        ANSWER_TIME = 45          
        NEXT_QUESTION_DELAY = 30  

        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=full_question_text,
            options=options,
            type=Poll.QUIZ,
            correct_option_id=question_data["correct_answer"],
            is_anonymous=False,
            open_period=ANSWER_TIME 
        )
        
        context.bot_data[message.poll.id] = {
            "chat_id": chat_id,
            "correct_answer": question_data["correct_answer"],
            "poll_id": message.poll.id
        }
        
        # --- SUCCESS ---
        quiz_data['current_question_index'] = current_index + 1
        context.bot_data[active_quiz_key] = quiz_data
        
        is_last_question = (current_index + 1) == quiz_length
        delay = ANSWER_TIME + 1 if is_last_question else NEXT_QUESTION_DELAY

        context.job_queue.run_once(
            lambda ctx: ctx.application.create_task(send_next_question_callback(ctx, chat_id)), 
            delay
        )
        
    # --- 2. SPECIFIC ERROR HANDLING ---
    except BadRequest as e:
        # This catches "Poll options length must not exceed 100" and other data errors
        logger.error(f"❌ Telegram Bad Request for Q{current_index + 1} (DB ID: {db_id}): {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Skipping a question due to format error (Option too long).")
        
        # Skip, DO NOT RETRY
        quiz_data['current_question_index'] = current_index + 1
        context.bot_data[active_quiz_key] = quiz_data
        context.job_queue.run_once(
            lambda ctx: ctx.application.create_task(send_next_question_callback(ctx, chat_id)), 
            2
        )

    except (NetworkError, TimedOut) as net_err:
        # This is for ACTUAL network issues
        logger.warning(f"⚠️ Network connection lost while sending Q{current_index+1}. Retrying... Error: {net_err}")
        
        # Retry logic
        context.job_queue.run_once(
            lambda ctx: ctx.application.create_task(send_next_question_callback(ctx, chat_id)), 
            5 
        )
        
    except Exception as e:
        logger.error(f"General Error sending question {current_index + 1}: {e}")
        # Skip
        quiz_data['current_question_index'] = current_index + 1
        context.bot_data[active_quiz_key] = quiz_data
        context.job_queue.run_once(
            lambda ctx: ctx.application.create_task(send_next_question_callback(ctx, chat_id)), 
            1
        )


async def send_next_question_callback(context: CallbackContext, chat_id: int) -> None:
    """Callback for the job queue to send the next question."""
    logger.info(f"send_next_question_callback called for chat {chat_id}")
    active_quiz_key = f'active_quiz_{chat_id}'
    quiz_data = context.bot_data.get(active_quiz_key)
    
    if quiz_data and quiz_data.get('quiz_active', False):
        await send_next_question(context, quiz_data)
    else:
        logger.warning(f"No active quiz found for chat {chat_id} in callback, or quiz is inactive.")

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a user's answer to a quiz poll to update their score."""
    poll_answer = update.poll_answer
    poll_id = poll_answer.poll_id
    user = poll_answer.user

    quiz_info = context.bot_data.get(poll_id)
    if not quiz_info:
        logger.warning(f"No quiz info found for poll_id: {poll_id}")
        return

    chat_id = quiz_info["chat_id"]
    correct_answer_id = quiz_info["correct_answer"]
    
    active_quiz_key = f'active_quiz_{chat_id}'
    quiz_data = context.bot_data.get(active_quiz_key, {})
    
    if not quiz_data.get('quiz_active', False):
        logger.warning(f"Received answer for inactive quiz. Chat_id: {chat_id}")
        return

    selected_option_ids = poll_answer.option_ids
    
    logger.info(f"User {user.first_name} selected option: {selected_option_ids}, correct answer: {correct_answer_id}")
    
    if selected_option_ids and selected_option_ids[0] == correct_answer_id:
        user_id = user.id
        if user_id not in quiz_data['scores']:
            quiz_data['scores'][user_id] = {'name': user.first_name, 'score': 0}
        
        quiz_data['scores'][user_id]['score'] += 1
        
        context.bot_data[active_quiz_key] = quiz_data
        
        logger.info(f"User {user.first_name} ({user_id}) answered correctly. New score: {quiz_data['scores'][user_id]['score']}")
    else:
        user_id = user.id
        if user_id not in quiz_data['scores']:
            quiz_data['scores'][user_id] = {'name': user.first_name, 'score': 0}
            context.bot_data[active_quiz_key] = quiz_data
        logger.info(f"User {user.first_name} ({user_id}) answered incorrectly.")


def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def format_scoreboard(scores: dict) -> str:
    """Formats the final scores into a readable string."""
    if not scores:
        return "🏁 **Quiz Over!**\n\nNo one answered correctly. Better luck next time!"

    users_with_points = {uid: data for uid, data in scores.items() if data['score'] > 0}
    
    if not users_with_points:
        return "🏁 **Quiz Over!**\n\nNo one answered correctly. Better luck next time!"

    sorted_scores = sorted(users_with_points.values(), key=lambda x: x['score'], reverse=True)
    
    scoreboard_text = "🏆 **Final Scoreboard** 🏆\n\n"
    for i, user_data in enumerate(sorted_scores):
        rank_emoji = ""
        if i == 0:
            rank_emoji = "🥇"
        elif i == 1:
            rank_emoji = "🥈"
        elif i == 2:
            rank_emoji = "🥉"
        
        safe_name = escape_markdown(user_data['name'])
        scoreboard_text += f"{rank_emoji} {i+1}. {safe_name}: {user_data['score']} point{'s' if user_data['score'] != 1 else ''}\n"
        
    return scoreboard_text

async def end_quiz(context: CallbackContext, quiz_data=None) -> None:
    """Ends the quiz and displays the scoreboard."""
    if quiz_data is None:
        logger.error("end_quiz called without quiz_data!")
        return
    
    if not quiz_data or not quiz_data.get('quiz_active', False):
        return

    chat_id = quiz_data.get('chat_id')
    result_text = format_scoreboard(quiz_data.get('scores', {}))
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=result_text,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send scoreboard due to network/error: {e}")
    
    active_quiz_key = f'active_quiz_{chat_id}'
    if active_quiz_key in context.bot_data:
        quiz_data['quiz_active'] = False
        context.bot_data[active_quiz_key] = quiz_data
        
    logger.info(f"Quiz ended for chat {chat_id}")

async def scoreboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the scoreboard for the current or last quiz."""
    chat_id = update.effective_chat.id
    active_quiz_key = f'active_quiz_{chat_id}'
    quiz_data = context.bot_data.get(active_quiz_key, {})
    
    if quiz_data.get('quiz_active', False):
        scores = quiz_data.get('scores', {})
        if scores:
            result_text = "📊 **Current Scores** 📊\n\n"
            sorted_scores = sorted(scores.values(), key=lambda x: x['score'], reverse=True)
            for i, user_data in enumerate(sorted_scores):
                rank_emoji = ""
                if i == 0 and user_data['score'] > 0:
                    rank_emoji = "🥇"
                elif i == 1 and user_data['score'] > 0:
                    rank_emoji = "🥈"
                elif i == 2 and user_data['score'] > 0:
                    rank_emoji = "🥉"
                result_text += f"{rank_emoji} {i+1}. {user_data['name']}: {user_data['score']} point{'s' if user_data['score'] != 1 else ''}\n"
            await update.message.reply_text(result_text, parse_mode='Markdown')
        else:
            await update.message.reply_text("No scores yet! The quiz is still in progress.")
    else:
        scores = quiz_data.get('scores', {}) 
        if scores:
            result_text = format_scoreboard(scores)
            await update.message.reply_text(result_text, parse_mode='Markdown')
        else:
            await update.message.reply_text("No quiz has been completed yet.")

async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the current quiz immediately."""
    chat = update.effective_chat
    user = update.effective_user

    if not user or not chat:
        return

    # --- Admin Check Logic ---
    if chat.type == 'private':
        await update.message.reply_text("This command only works in groups!")
        return

    try:
        chat_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
    except Exception as e:
        logger.error(f"Error checking chat member status: {e}")
        return

    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("Sorry, only group admins can stop the quiz! ⛔️")
        return
    # --- End Admin Check Logic ---

    chat_id = update.effective_chat.id
    active_quiz_key = f'active_quiz_{chat_id}'
    quiz_data = context.bot_data.get(active_quiz_key)

    if quiz_data and quiz_data.get('quiz_active', False):
        await update.message.reply_text("🛑 Quiz stopped by admin.")
        # end_quiz handles setting quiz_active = False and showing the final scoreboard
        await end_quiz(context, quiz_data)
    else:
        await update.message.reply_text("There is no active quiz to stop.")

# --- Main Application Logic ---

def main() -> None:
    """This function starts the bot."""
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("Please set the BOT_TOKEN environment variable in the .env file.")

    # --- PERSISTENCE SETUP (Data saved to quiz_bot_data.pickle) ---
    my_persistence = PicklePersistence(filepath='quiz_bot_data.pickle')

    # --- NETWORK REQUEST SETUP ---
    # Create the request object with the custom timeouts here
    request = HTTPXRequest(connect_timeout=30, read_timeout=30)

    # Pass the request object to the builder
    application = Application.builder().token(TOKEN).persistence(my_persistence).request(request).build()

    for command in QUIZ_DATABASES.keys():
        application.add_handler(CommandHandler(command, bot))

    application.add_handler(CommandHandler("scoreboard", scoreboard))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    application.add_handler(CommandHandler("kill", stop_quiz))

    logger.info("Starting bot...")
    
    # Run polling without arguments (timeouts are handled by HTTPXRequest now)
    application.run_polling()

if __name__ == "__main__":
    main()