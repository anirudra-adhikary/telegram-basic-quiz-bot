import logging
import os
import json
import random

from dotenv import load_dotenv
from telegram import Update, Poll
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
    CallbackContext,
)

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()  # Load environment variables from .env file

# --- Bot Handlers ---

async def startquiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a new quiz in the group chat, only if initiated by an admin."""
    chat = update.effective_chat
    user = update.effective_user

    if not user or not chat:
        logger.warning("Could not get user or chat from update.")
        return
        
    # --- Admin Check Logic ---
    
    # 1. Check if we are in a group or supergroup
    if chat.type == 'private':
        await update.message.reply_text("This command only works in groups! 😅")
        return # Stop execution

    # 2. Get the user's status in the group
    try:
        chat_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
    except Exception as e:
        logger.error(f"Error checking chat member status: {e}")
        await update.message.reply_text("I couldn't check your permissions. 😬\nMake sure I have admin rights to see other members.")
        return

    # 3. Check if the status is 'administrator' or 'creator'
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("Sorry, only group admins can start a quiz! ⛔️")
        return # Stop execution if not an admin
        
    logger.info(f"Admin check passed for user {user.id} in chat {chat.id}. Starting quiz...")
    # --- End Admin Check Logic ---

    chat_id = update.effective_chat.id

    # Check if quiz is already active using bot_data
    active_quiz_key = f'active_quiz_{chat_id}'
    if context.bot_data.get(active_quiz_key, {}).get('quiz_active', False):
        await update.message.reply_text(
            "A quiz is already in progress in this group! 😮\n"
            "Wait for it to finish before starting a new one."
        )
        return

    try:
        with open('questions.json', 'r') as f:
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
    
    # Store quiz data in bot_data for global access
    quiz_data = {
        'quiz_questions': selected_questions,
        'current_question_index': 0,
        'scores': {},
        'quiz_active': True,
        'chat_id': chat_id
    }
    
    # Store in bot_data (our single source of truth)
    context.bot_data[active_quiz_key] = quiz_data

    await update.message.reply_text(
        f"🎯 **Quiz Started!**\n\n"
        f"• {QUIZ_LENGTH} random questions\n"
        "• 30 seconds per question\n\n"
        "Good luck! 🍀",
        parse_mode='Markdown'
    )

    # Manually pass the quiz_data to the first call
    await send_next_question(context, quiz_data)

async def send_next_question(context: CallbackContext, quiz_data=None) -> None:
    """Sends the next question (and code snippet, if any) or ends the quiz."""
    if quiz_data is None:
        logger.warning("send_next_question called without quiz_data (likely from job). This should be handled by the callback.")
        return
        
    if not quiz_data:
        logger.warning("No quiz data found in send_next_question")
        return

    chat_id = quiz_data.get('chat_id')
    current_index = quiz_data.get('current_question_index', 0)
    questions = quiz_data.get('quiz_questions', [])
    active_quiz_key = f'active_quiz_{chat_id}'
    
    logger.info(f"Current question index: {current_index}, Total questions: {len(questions)}")

    if current_index >= len(questions):
        logger.info(f"Quiz completed! Ending quiz for chat {chat_id}")
        await end_quiz(context, quiz_data)
        return

    question_data = questions[current_index]
    
    # Debug: Log the question and correct answer
    logger.info(f"Sending question {current_index + 1}: {question_data['question']}")
    logger.info(f"Correct answer index: {question_data['correct_answer']}")
    
    try:
        # --- NEW CODE BLOCK ---
        # Check if there is a code snippet and send it first
        if "code_snippet" in question_data and question_data["code_snippet"]:
            code = question_data["code_snippet"]
            lang = question_data.get("language", "") # Get language, default to empty string
            
            # Format the code for Telegram's Markdown
            # We use MarkdownV2, which requires escaping, but is safer for code
            # Note: The code itself should not contain special markdown chars
            # or they must be escaped.
            # For simplicity, we'll just wrap it.
            # A more robust solution would escape characters, but let's try this first.
            
            formatted_code_text = f"```{lang}\n{code}\n```"
            
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=formatted_code_text,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as md_e:
                logger.warning(f"MarkdownV2 failed ({md_e}), trying plain text...")
                # Fallback to plain text if Markdown fails
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Code:\n{code}"
                )
        # --- END NEW CODE BLOCK ---

        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Question {current_index + 1}/{len(questions)}: {question_data['question']}",
            options=question_data["options"],
            type=Poll.QUIZ,
            correct_option_id=question_data["correct_answer"],
            # explanation = question_data["explanation"],
            is_anonymous=False,
            open_period=30
        )
        
        # Store poll info in bot_data for the answer handler
        context.bot_data[message.poll.id] = {
            "chat_id": chat_id,
            "correct_answer": question_data["correct_answer"],
            "poll_id": message.poll.id
        }
        
        # Update the current question index in quiz_data
        quiz_data['current_question_index'] = current_index + 1
        
        # Update bot_data (our single source of truth)
        context.bot_data[active_quiz_key] = quiz_data
        
        logger.info(f"Question {current_index + 1} sent successfully. Next index: {quiz_data['current_question_index']}")
        
        # Schedule next question
        context.job_queue.run_once(
            lambda ctx: ctx.application.create_task(send_next_question_callback(ctx, chat_id)), 
            31  # Give a 1-second buffer after the poll closes
        )
        
    except Exception as e:
        logger.error(f"Error sending poll or code snippet: {e}")

async def send_next_question_callback(context: CallbackContext, chat_id: int) -> None:
    """Callback for the job queue to send the next question."""
    logger.info(f"send_next_question_callback called for chat {chat_id}")
    active_quiz_key = f'active_quiz_{chat_id}'
    quiz_data = context.bot_data.get(active_quiz_key)
    
    if quiz_data and quiz_data.get('quiz_active', False):
        # Pass the quiz_data explicitly
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
    
    # Get quiz data from bot_data
    active_quiz_key = f'active_quiz_{chat_id}'
    quiz_data = context.bot_data.get(active_quiz_key, {})
    
    if not quiz_data.get('quiz_active', False):
        # This can happen if an answer comes in after the quiz has ended
        logger.warning(f"Received answer for inactive quiz. Chat_id: {chat_id}")
        return

    selected_option_ids = poll_answer.option_ids
    
    # Debug logging
    logger.info(f"User {user.first_name} selected option: {selected_option_ids}, correct answer: {correct_answer_id}")
    
    # Check if the user selected the correct option
    if selected_option_ids and selected_option_ids[0] == correct_answer_id:
        user_id = user.id
        # Initialize user score if not exists
        if user_id not in quiz_data['scores']:
            quiz_data['scores'][user_id] = {'name': user.first_name, 'score': 0}
        
        # Update score
        quiz_data['scores'][user_id]['score'] += 1
        
        # Update bot_data
        context.bot_data[active_quiz_key] = quiz_data
        
        logger.info(f"User {user.first_name} ({user_id}) answered correctly. New score: {quiz_data['scores'][user_id]['score']}")
    else:
        # Ensure user exists in scores even if they answered wrong (for scoreboard)
        user_id = user.id
        if user_id not in quiz_data['scores']:
            quiz_data['scores'][user_id] = {'name': user.first_name, 'score': 0}
            # Update bot_data
            context.bot_data[active_quiz_key] = quiz_data
        logger.info(f"User {user.first_name} ({user_id}) answered incorrectly.")

def format_scoreboard(scores: dict) -> str:
    """Formats the final scores into a readable string."""
    if not scores:
        return "🏁 **Quiz Over!**\n\nNo one answered correctly. Better luck next time!"

    # Filter out users with 0 points for final scoreboard
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
        
        scoreboard_text += f"{rank_emoji} {i+1}. {user_data['name']}: {user_data['score']} point{'s' if user_data['score'] != 1 else ''}\n"
        
    return scoreboard_text

async def end_quiz(context: CallbackContext, quiz_data=None) -> None:
    """Ends the quiz and displays the scoreboard."""
    if quiz_data is None:
        # This should ideally not be called without quiz_data
        # If it is, we have to find it, but it's risky
        logger.error("end_quiz called without quiz_data!")
        return
    
    if not quiz_data or not quiz_data.get('quiz_active', False):
        return

    chat_id = quiz_data.get('chat_id')
    result_text = format_scoreboard(quiz_data.get('scores', {}))
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=result_text,
        parse_mode='Markdown'
    )
    
    # Clean up
    active_quiz_key = f'active_quiz_{chat_id}'
    if active_quiz_key in context.bot_data:
        # Keep scores but mark quiz as inactive
        quiz_data['quiz_active'] = False
        context.bot_data[active_quiz_key] = quiz_data
        
    logger.info(f"Quiz ended for chat {chat_id}")

async def scoreboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the scoreboard for the current or last quiz."""
    chat_id = update.effective_chat.id
    active_quiz_key = f'active_quiz_{chat_id}'
    quiz_data = context.bot_data.get(active_quiz_key, {})
    
    if quiz_data.get('quiz_active', False):
        # Show current scores during active quiz
        scores = quiz_data.get('scores', {})
        if scores:
            result_text = "📊 **Current Scores** 📊\n\n"
            # Show all users, even with 0 points during active quiz
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
        # This is the 'quiz is finished' case
        # We read from bot_data, which holds the last quiz state
        scores = quiz_data.get('scores', {}) # quiz_data is already from bot_data
        if scores:
            result_text = format_scoreboard(scores)
            await update.message.reply_text(result_text, parse_mode='Markdown')
        else:
            await update.message.reply_text("No quiz has been completed yet. Use /startquiz to begin a new one.")


# --- Main Application Logic ---

def main() -> None:
    """This function starts the bot."""
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("Please set the BOT_TOKEN environment variable in the .env file.")

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("startquiz", startquiz))
    application.add_handler(CommandHandler("scoreboard", scoreboard))
    # application.add_handler(CommandHandler("stopquiz", end_quiz(context, quiz_data)))
    
    application.add_handler(PollAnswerHandler(handle_poll_answer))

    logger.info("Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()