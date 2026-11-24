QUESTIONS = {
    "best_action": {
        "type": "choice",
        "instructions": "What kind of reply fits the incoming message and the user's stated goal?",
        "criteria": {
            "check_history": "Verify the facts first.",
            "apologize": "Apologize for an identified mistake.",
            "give_commitment": "Give a commitment authorized by the user.",
            "explain": "Explain what happened.",
            "acknowledge": "Acknowledge the message.",
            "say_less": "Give a brief response.",
            "make_plan": "Propose or confirm logistics.",
        },
    }
}
