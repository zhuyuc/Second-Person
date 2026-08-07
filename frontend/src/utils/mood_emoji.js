export const MOOD_EMOJI = {
  joy: "😄", pleased: "🙂", excited: "🤩", warm: "🥰",
  grateful: "🙏", angry: "😠", irritated: "😤", frustrated: "😩",
  indignant: "😤", hurt: "😢", sad: "😢", melancholy: "🥺",
  compassionate: "🤝", remorseful: "😔", apologetic: "🙇",
  fearful: "😰", anxious: "😟", cautious: "🤔", defensive: "🛡️",
  affectionate: "💛", caring: "🤗", trusting: "🫶",
  disgusted: "😒", disdainful: "🙄", curious: "🧐",
  aspiring: "💪", competitive: "🔥", playful: "😜",
  calm: "😌", determined: "🎯", surprised: "😮",
  confused: "😵", bored: "😑", tired: "😴",
  proud: "🥇", humble: "🙇", relieved: "😌",
  ashamed: "😳", self_critical: "😔", neutral: "😐",
  hopeful: "🌟", lonely: "💧", guilty: "😞",
  peaceful: "🕊️", wary: "👀", eager: "🤗",
};

export const moodEmoji = (mood) => MOOD_EMOJI[mood] || "😐";
