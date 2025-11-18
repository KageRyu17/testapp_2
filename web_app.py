import os
import json
import math
import requests
from flask import Flask, request, render_template, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_API_KEY", "supersecretkey")

# --- CONFIGURAZIONE DATABASE ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///flashcards.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- MODELLI DATABASE ---
class Deck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    topic = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    cards = db.relationship('Flashcard', backref='deck', lazy=True, cascade="all, delete-orphan")

class Flashcard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    front = db.Column(db.Text, nullable=False)
    back = db.Column(db.Text, nullable=False)
    deck_id = db.Column(db.Integer, db.ForeignKey('deck.id'), nullable=False)

# Inizializza il DB
with app.app_context():
    db.create_all()

# --- CONFIGURAZIONE GEMINI ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "INSERISCI_LA_TUA_GEMINI_API_KEY_QUI")
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# --- FUNZIONI DI SUPPORTO ---

def clean_gemini_json(text):
    """
    Pulisce la risposta di Gemini per estrarre il JSON puro.
    Taglia tutto ciò che c'è prima della prima { o [ e dopo l'ultima } o ].
    Risolve l'errore 'Extra data'.
    """
    text = text.strip()
    
    # Rimuove markdown ```json ... ``` se presente
    if "```" in text:
        text = text.replace("```json", "").replace("```", "")
    
    # Cerca l'inizio e la fine dell'oggetto JSON o dell'Array
    start_brace = text.find("{")
    start_bracket = text.find("[")
    
    # Determina dove inizia il JSON (se è un oggetto o un array)
    start = -1
    end = -1
    
    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        # È un oggetto (Quiz)
        start = start_brace
        end = text.rfind("}")
    elif start_bracket != -1:
        # È un array (Flashcards)
        start = start_bracket
        end = text.rfind("]")
        
    if start != -1 and end != -1:
        return text[start:end+1]
        
    return text # Ritorna il testo originale se non trova JSON (causerà errore nel try/except dopo)

def generate_quiz_logic(program_text: str, num_questions: int):
    """Genera Quiz mantenendo la logica 50% MCQ e 50% Open."""
    if num_questions <= 0:
        raise ValueError("Il numero di domande deve essere > 0")

    # Calcolo mix domande
    desired_mcq = math.ceil(num_questions * 0.5)
    desired_open = num_questions - desired_mcq

    full_prompt = f"""
    Sei un generatore di quiz in italiano per studenti universitari.
    Crea domande a partire dal testo fornito.
    
    OBIETTIVO:
    - Genera ESATTAMENTE {num_questions} domande.
    - Circa {desired_mcq} domande devono essere a risposta multipla ("mcq").
    - Circa {desired_open} domande devono essere a completamento ("open") con una sola parola.
    - NO formule, solo teoria.

    FORMATO DI USCITA (JSON PURO):
    {{
      "questions": [
        {{
          "text": "domanda...",
          "qtype": "mcq",
          "options": ["A", "B", "C", "D"],
          "answer": "A"
        }},
        {{
          "text": "domanda...",
          "qtype": "open",
          "options": null,
          "answer": "parola_singola"
        }}
      ]
    }}
    
    Non aggiungere altro testo prima o dopo il JSON.

    TESTO:
    {program_text}
    """
    
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
    resp = requests.post(GEMINI_URL, headers={"Content-Type": "application/json"}, json=payload)
    resp.raise_for_status()
    
    try:
        # Estrazione
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        # Pulizia aggressiva
        text = clean_gemini_json(text)
        
        parsed = json.loads(text)
        
        # Se restituisce una lista invece di un oggetto (capita a volte), gestiamolo
        if isinstance(parsed, list):
            return parsed
        elif "questions" in parsed:
            return parsed["questions"]
        else:
            raise RuntimeError("Formato JSON imprevisto (manca chiave 'questions')")

    except Exception as e:
        # Log per debug (opzionale, stampa su console)
        print(f"Errore RAW text da Gemini: {text}") 
        raise RuntimeError(f"Errore parsing Gemini: {e}")

def generate_flashcards_logic(program_text: str, num_cards: int):
    """Genera flashcard (Fronte/Retro)."""
    prompt = f"""
    Crea {num_cards} flashcard basate su questo testo: "{program_text}".
    
    STRUTTURA RICHIESTA (JSON ARRAY):
    [
        {{"front": "Domanda o Concetto", "back": "Risposta o Definizione"}},
        {{"front": "...", "back": "..."}}
    ]
    
    Output SOLO JSON valido. Niente testo introduttivo.
    """
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = requests.post(GEMINI_URL, headers={"Content-Type": "application/json"}, json=payload)
    resp.raise_for_status()
    
    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = clean_gemini_json(text)
        cards = json.loads(text)
        return cards
    except Exception as e:
        raise RuntimeError(f"Errore generazione flashcard: {e}")

# --- ROTTE ---

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate_quiz():
    program_text = request.form.get("program_text", "").strip()
    num_input = request.form.get("num_questions", "").strip()
    action = request.form.get("action", "quiz") # Default a quiz se manca

    if not program_text:
        flash("Devi incollare il contenuto del programma.")
        return redirect(url_for("index"))
    
    if not num_input.isdigit():
        flash("Numero non valido.")
        return redirect(url_for("index"))
    
    count = int(num_input)
    if count <= 0 or count > 50:
        flash("Il numero deve essere tra 1 e 50.")
        return redirect(url_for("index"))
    
    if action == "flashcards":
        # --- PERCORSO FLASHCARDS ---
        try:
            cards_data = generate_flashcards_logic(program_text, count)
            
            # Titolo breve per il salvataggio
            title = program_text[:50].replace("\n", " ") + "..." 
            
            new_deck = Deck(topic=title)
            db.session.add(new_deck)
            db.session.commit()
            
            for c in cards_data:
                card = Flashcard(front=c['front'], back=c['back'], deck=new_deck)
                db.session.add(card)
            db.session.commit()
            
            return redirect(url_for('view_flashcards', deck_id=new_deck.id))
            
        except Exception as e:
            flash(f"Errore Flashcard: {e}")
            return redirect(url_for("index"))
            
    else:
        # --- PERCORSO QUIZ ---
        try:
            questions = generate_quiz_logic(program_text, count)
            session["questions"] = questions
            return render_template("quiz.html", questions=questions)
        except Exception as e:
            flash(f"Errore Quiz: {e}")
            return redirect(url_for("index"))

@app.route("/submit", methods=["POST"])
def submit_quiz():
    questions = session.get("questions")
    if not questions:
        flash("Nessun quiz attivo.")
        return redirect(url_for("index"))
        
    score = 0.0
    correct = 0; wrong = 0; blank = 0; details = []
    
    for i, q in enumerate(questions):
        ans = request.form.get(f"q{i}", "").strip()
        is_correct = False
        result = "blank"
        
        if not ans:
            blank += 1
        else:
            if q["qtype"] == "mcq":
                is_correct = (ans == q["answer"])
            else:
                # Per le open, controllo case-insensitive
                is_correct = (ans.lower() == q["answer"].lower())
            
            if is_correct:
                score += 1.0; correct += 1; result = "correct"
            else:
                score -= 0.1; wrong += 1; result = "wrong"
        
        details.append({
            "text": q["text"], 
            "user_answer": ans, 
            "correct_answer": q["answer"], 
            "result": result
        })

    session.pop("questions", None) # Pulisce sessione dopo submit
    return render_template("result.html", total=len(questions), correct=correct, wrong=wrong, blank=blank, score=f"{score:.2f}", details=details)

# --- NUOVE ROTTE FLASHCARD ---

@app.route('/flashcards/<int:deck_id>')
def view_flashcards(deck_id):
    deck = Deck.query.get_or_404(deck_id)
    return render_template('flashcard_player.html', deck=deck)

@app.route('/saved_flashcards')
def saved_flashcards():
    decks = Deck.query.order_by(Deck.created_at.desc()).all()
    return render_template('saved_flashcards.html', decks=decks)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
