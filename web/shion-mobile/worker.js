// web/shion-mobile/worker.js — moteur d'inférence de Shion Mobile.
//
// Shion-Nano (le petit réseau maison de shion/model.py, ~468 000 paramètres)
// a été abandonné ici : trop jeune pour produire des réponses compréhensibles
// (voir web/shion-mobile/index.html pour le contexte affiché à l'utilisateur).
// À la place, Shion Mobile fait tourner un VRAI modèle DeepSeek pré-entraîné
// -- DeepSeek-R1-Distill-Qwen-1.5B -- entièrement dans ce Web Worker via
// Transformers.js + ONNX Runtime Web (backend WebGPU). Aucun serveur,
// aucune requête réseau après le téléchargement initial des poids.
//
// Repris (structure Singleton, contrat de messages, découpage
// thinking/answering) du modèle de référence officiel Hugging Face :
// https://github.com/huggingface/transformers.js-examples/tree/main/deepseek-r1-webgpu
import {
  AutoTokenizer,
  AutoModelForCausalLM,
  TextStreamer,
  InterruptableStoppingCriteria,
} from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3";

async function check() {
  try {
    if (!("gpu" in navigator)) {
      throw new Error("WebGPU indisponible dans ce navigateur.");
    }
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) {
      throw new Error("WebGPU indisponible (aucun adaptateur GPU trouvé).");
    }
  } catch (e) {
    self.postMessage({ status: "error", data: e.toString() });
  }
}

// Singleton paresseux : le modèle n'est chargé qu'à la première demande
// explicite de l'utilisateur (voir le bouton "Charger le modèle" côté UI),
// jamais au chargement de la page -- le téléchargement fait ~1,4 Go.
class TextGenerationPipeline {
  static model_id = "onnx-community/DeepSeek-R1-Distill-Qwen-1.5B-ONNX";

  static async getInstance(progress_callback = null) {
    this.tokenizer ??= AutoTokenizer.from_pretrained(this.model_id, {
      progress_callback,
    });
    this.model ??= AutoModelForCausalLM.from_pretrained(this.model_id, {
      dtype: "q4f16",
      device: "webgpu",
      progress_callback,
    });
    return Promise.all([this.tokenizer, this.model]);
  }
}

const stopping_criteria = new InterruptableStoppingCriteria();
let past_key_values_cache = null;

async function generate(messages) {
  const [tokenizer, model] = await TextGenerationPipeline.getInstance();

  const inputs = tokenizer.apply_chat_template(messages, {
    add_generation_prompt: true,
    return_dict: true,
  });

  // DeepSeek-R1-Distill "réfléchit" entre <think> et </think> avant de
  // répondre -- on suit ces deux jetons pour distinguer, côté UI, le
  // brouillon de raisonnement (affiché en repliable) de la réponse finale.
  const [START_THINKING_TOKEN_ID, END_THINKING_TOKEN_ID] = tokenizer.encode(
    "<think></think>",
    { add_special_tokens: false },
  );

  let state = "thinking";
  let startTime;
  let numTokens = 0;
  let tps;
  const token_callback_function = (tokens) => {
    startTime ??= performance.now();
    if (numTokens++ > 0) {
      tps = (numTokens / (performance.now() - startTime)) * 1000;
    }
    if (tokens[0] == END_THINKING_TOKEN_ID) {
      state = "answering";
    }
  };
  const callback_function = (output) => {
    self.postMessage({ status: "update", output, tps, numTokens, state });
  };

  const streamer = new TextStreamer(tokenizer, {
    skip_prompt: true,
    skip_special_tokens: true,
    callback_function,
    token_callback_function,
  });

  self.postMessage({ status: "start" });

  const { past_key_values, sequences } = await model.generate({
    ...inputs,
    do_sample: false,
    max_new_tokens: 2048,
    streamer,
    stopping_criteria,
    return_dict_in_generate: true,
  });
  past_key_values_cache = past_key_values;

  const decoded = tokenizer.batch_decode(sequences, {
    skip_special_tokens: true,
  });

  self.postMessage({ status: "complete", output: decoded });
}

async function load() {
  self.postMessage({ status: "loading", data: "Téléchargement du modèle…" });

  const [tokenizer, model] = await TextGenerationPipeline.getInstance((x) => {
    self.postMessage(x);
  });

  self.postMessage({
    status: "loading",
    data: "Compilation des shaders et préchauffage…",
  });

  // Passe factice pour compiler les shaders WebGPU avant le premier vrai
  // message -- sans ça, la toute première réponse de l'utilisateur paie
  // seule ce coût de compilation et semble figée plusieurs secondes.
  const inputs = tokenizer("a");
  await model.generate({ ...inputs, max_new_tokens: 1 });
  self.postMessage({ status: "ready" });
}

self.addEventListener("message", async (e) => {
  const { type, data } = e.data;
  switch (type) {
    case "check":
      check();
      break;
    case "load":
      load();
      break;
    case "generate":
      stopping_criteria.reset();
      generate(data);
      break;
    case "interrupt":
      stopping_criteria.interrupt();
      break;
    case "reset":
      past_key_values_cache = null;
      stopping_criteria.reset();
      break;
  }
});
