/* QBG One - all the friendly wording in one place.
 *
 * Non-developers can edit this file. Change the text inside the quotes, or add
 * more lines to a list; do not rename the keys or the {things in braces}.
 * {name} becomes the person's name, {count} a number, {minutes} minutes,
 * {hours} hours, {days} days. Two message sets: "english" and "hinglish"
 * (the language is chosen in Settings).
 */
window.QBG_MESSAGES = {
  english: {
    welcome_title: "Welcome to QBG One 👋",
    welcome_body: "Upload question papers, translate and tag them, and send them to QBG - all in one place.",
    welcome_button: "Let's start",

    greeting_morning: "Good morning ☀️",
    greeting_afternoon: "Good afternoon",
    greeting_evening: "Good evening",
    greeting_night: "Working late? Don't forget to rest 🌙",

    /* {greeting} is one of the four above; {name} is dropped when unknown. */
    welcome_back: "{greeting}, {name}! Ready for today's questions?",
    welcome_back_noname: "{greeting}! Ready for today's questions?",

    encouragements: [
      "You're doing great today.",
      "Nice pace - keep it going.",
      "Chai break? You've earned it ☕",
      "That's solid work.",
      "One step at a time - you've got this."
    ],

    first_upload: "Your first upload is done! 🎉",
    batch_uploaded: "{count} questions uploaded in {minutes}.",
    batch_uploaded_plain: "{count} questions uploaded.",
    translation_done: "Translation ready ✅",
    tagging_done: "Tagging done ✅",
    milestone_uploaded: "{count} questions uploaded so far - amazing! 🎉",
    milestone_hours: "QBG One has saved you about {hours} hours so far! ⏱️",
    milestone_streak: "{days} working days with QBG One - what a streak! 🔥",

    close: "Close"
  },

  hinglish: {
    welcome_title: "QBG One me aapka swagat hai 👋",
    welcome_body: "Question papers upload karo, translate aur tag karo, aur QBG pe bhejo - sab ek jagah.",
    welcome_button: "Chalo shuru karein",

    greeting_morning: "Good morning ☀️",
    greeting_afternoon: "Good afternoon",
    greeting_evening: "Good evening",
    greeting_night: "Der raat kaam? Thoda aaram bhi zaroori hai 🌙",

    welcome_back: "{greeting}, {name}! Aaj ke questions ke liye taiyaar?",
    welcome_back_noname: "{greeting}! Aaj ke questions ke liye taiyaar?",

    encouragements: [
      "Aaj aap bahut accha kaam kar rahe ho.",
      "Badhiya speed - aise hi chalte raho.",
      "Chai break? Aapne earn kiya hai ☕",
      "Solid kaam ho raha hai.",
      "Ek ek karke - ho jayega."
    ],

    first_upload: "Aapka pehla upload ho gaya! 🎉",
    batch_uploaded: "{count} questions {minutes} me upload ho gaye.",
    batch_uploaded_plain: "{count} questions upload ho gaye.",
    translation_done: "Translation taiyaar ✅",
    tagging_done: "Tagging ho gayi ✅",
    milestone_uploaded: "Ab tak {count} questions upload - kamaal! 🎉",
    milestone_hours: "QBG One ne aapke lagbhag {hours} ghante bachaye hain! ⏱️",
    milestone_streak: "{days} working days QBG One ke saath - zabardast streak! 🔥",

    close: "Band karein"
  }
};
