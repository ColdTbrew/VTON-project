import os
import sys
import argparse

import torch
import torch.nn as nn
import torch.optim as optim
from torch.nn import functional as F
from torch.utils.data import Dataset, DataLoader
import torchvision.transforms as transforms

from peft import get_peft_model, LoraConfig, TaskType

from diffusers import DDPMScheduler
from diffusers.image_processor import VaeImageProcessor


from PIL import Image
from tqdm import tqdm

#import wandb

# 윈도우에서 wandb 사용 시 필요한 코드
# import resource
# if not hasattr(resource, "getpagesize"):
#     resource.getpagesize = lambda: 4096
sys.path.append(os.path.abspath(os.path.join(os.getcwd(),"CatVTON")))
from model.pipeline import CatVTONPipeline
from utils import compute_vae_encodings, tensor_to_image, numpy_to_pil

def parse_args():
    parser = argparse.ArgumentParser(description="LoRA Fine-tuning for Latent Diffusion based CatVTON")
    parser.add_argument("--data_root_path", type=str, required=True, help="Path to the training dataset.")
    parser.add_argument("--output_dir", type=str, default="output", help="Directory to save checkpoints.")
    parser.add_argument("--num_epochs", type=int, default=10, help="Number of training epochs.")
    parser.add_argument("--batch_size", type=int, default=8, help="Batch size for training.")
    parser.add_argument("--lr", type=float, default=1e-4, help="Learning rate.")
    parser.add_argument("--lora_rank", type=int, default=4, help="LoRA rank parameter.")
    parser.add_argument("--seed", type=int, default=555, help="Random seed for reproducibility.")
    parser.add_argument("--eval_pair", default=False, help="Evaluate on paired images.")
    parser.add_argument("--height", type=int, default=1024, help="Image height.")
    parser.add_argument("--width", type=int, default=768, help="Image width.")
    # latent diffusion 관련 파라미터
    parser.add_argument("--num_train_timesteps", type=int, default=1000, help="Number of diffusion steps for training.")
    parser.add_argument("--use_tf32", default=False, help="Use TF32 precision for training.")
    parser.add_argument("--attn_ckpt_version", type=str, default="mix", help="Version of the attention checkpoint.")
    parser.add_argument("--guidance_scale", type=float, default=2.5, help="Guidance scale for the diffusion model.")
    parser.add_argument(
        "--num_inference_steps",
        type=int,
        default=50,
        help="Number of inference steps to perform.",
    )
    parser.add_argument("--use_fp16", default=True, help="Use FP16 precision for training.")
    args = parser.parse_args()
    return args


class TrainDataset(Dataset):
    def __init__(self, args):
        self.args = args
        self.vae_processor = VaeImageProcessor(vae_scale_factor=8)
        self.mask_processor = VaeImageProcessor(
            vae_scale_factor=8, 
            do_normalize=False, 
            do_binarize=True, 
            do_convert_grayscale=True
        )
        self.data = self.load_data()

    def load_data(self):
        return []

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        data = self.data[idx]
        person, cloth, mask = [Image.open(data[key]) for key in ['person', 'cloth', 'mask']]
        return {
            'index': idx,
            'person_name': data['person_name'],
            'person': self.vae_processor.preprocess(person, self.args.height, self.args.width)[0],
            'cloth': self.vae_processor.preprocess(cloth, self.args.height, self.args.width)[0],
            'mask': self.mask_processor.preprocess(mask, self.args.height, self.args.width)[0]
        }


# class VITONHDTestDataset(TrainDataset):
#     def load_data(self):
#         pair_txt = os.path.join(self.args.data_root_path, 'test_pairs_unpaired.txt')
#         assert os.path.exists(pair_txt), f"File {pair_txt} does not exist."
#         with open(pair_txt, 'r') as f:
#             lines = f.readlines()
#         self.args.data_root_path = os.path.join(self.args.data_root_path, "test")
#         output_dir = os.path.join(
#             self.args.output_dir, 
#             "vitonhd", 
#             'unpaired' if not self.args.eval_pair else 'paired'
#         )
#         data = []
#         for line in lines:
#             person_img, cloth_img = line.strip().split(" ")
#             if os.path.exists(os.path.join(output_dir, person_img)):
#                 continue
#             if self.args.eval_pair:
#                 cloth_img = person_img
#             data.append({
#                 'person_name': person_img,
#                 'person': os.path.join(self.args.data_root_path, 'image', person_img),
#                 'cloth': os.path.join(self.args.data_root_path, 'cloth', cloth_img),
#                 'mask': os.path.join(self.args.data_root_path, 'agnostic-mask', person_img.replace('.jpg', '_mask.png')),
#             })
#         return data

class VITONHDTrainDataset(TrainDataset):
    def load_data(self):
        pair_txt = os.path.join(self.args.data_root_path, 'train_pairs_sample.txt')
        assert os.path.exists(pair_txt), f"File {pair_txt} does not exist."
        with open(pair_txt, 'r') as f:
            lines = f.readlines()
        self.args.data_root_path = os.path.join(self.args.data_root_path, "train")
        output_dir = os.path.join(
            self.args.output_dir, 
            "vitonhd", 
            'unpaired' if not self.args.eval_pair else 'paired'
        )
        data = []
        for line in lines:
            person_img, cloth_img = line.strip().split(" ")
            if os.path.exists(os.path.join(output_dir, person_img)):
                continue
            if self.args.eval_pair:
                cloth_img = person_img
            data.append({
                'person_name': person_img,
                'person': os.path.join(self.args.data_root_path, 'image', person_img),
                'cloth': os.path.join(self.args.data_root_path, 'cloth', cloth_img),
                'mask': os.path.join(self.args.data_root_path, 'agnostic-v3.2', person_img),
            })
        return data

class Custom_VITONHDTrainDataset(TrainDataset):
    def load_data(self):
        pair_txt = os.path.join(self.args.data_root_path, 'train_unpair.txt')
        assert os.path.exists(pair_txt), f"File {pair_txt} does not exist."
        with open(pair_txt, 'r') as f:
            lines = f.readlines()
        #self.args.data_root_path = os.path.join(self.args.data_root_path, "train")
        output_dir = os.path.join(
            self.args.output_dir, 
            "vitonhd", 
            'unpaired' if not self.args.eval_pair else 'paired'
        )
        data = []
        for line in lines:
            person_img, cloth_img = line.strip().split(" ")
            if os.path.exists(os.path.join(output_dir, person_img)):
                continue
            if self.args.eval_pair:
                cloth_img = person_img
            data.append({
                'person_name': person_img,
                'person': os.path.join(self.args.data_root_path, 'image', person_img),
                'cloth_l': os.path.join(self.args.data_root_path, 'cloth', 'lower_img' ,cloth_img),
                'cloth': os.path.join(self.args.data_root_path, 'cloth', 'upper_img' ,cloth_img),
                'mask_l': os.path.join(self.args.data_root_path, 'image_with_lower_agnostic', person_img),
                'mask': os.path.join(self.args.data_root_path, 'image_with_upper_agnostic', person_img)
            })
        return data

def main():
    args = parse_args()
    torch.manual_seed(args.seed)
    # wandb.init(project="VTON-project", config=vars(args))
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # 1. 파이프라인과 모델 초기화 (여러분의 환경에 맞게 수정)
    base_ckpt = "booksforcharlie/stable-diffusion-inpainting"
    attn_ckpt = "zhengchong/CatVTON"
    attn_ckpt_version = "mix"

    pipeline = CatVTONPipeline(
        base_ckpt, 
        attn_ckpt,
        attn_ckpt_version,
        weight_dtype=torch.float16,
        device="cuda",
        skip_safety_check=True,
    )

    # fine-tuning 대상 모델 (예: UNet) 추출
    model = pipeline.unet 
    model.to(device)
    print("기존 unet 학습 파라미터 수:",
          sum(p.numel() for p in model.parameters() if p.requires_grad))

    # 2. LoRA 설정 및 적용
    lora_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_rank,
        lora_dropout=0.1,
        target_modules=["to_q", "to_k", "to_v"],  
    )
    model = get_peft_model(model, lora_config)
    print("LoRA 적용 완료. 현재 학습 파라미터 수:",
          sum(p.numel() for p in model.parameters() if p.requires_grad))
    


    # 4. 옵티마이저 및 손실 함수 정의
    optimizer = optim.AdamW(model.parameters(), lr=args.lr)
    loss_fn = nn.MSELoss()  # diffusion 학습에서는 주로 예측한 노이즈와 실제 노이즈 간의 MSE를 사용

    #dataset = VITONHDTrainDataset(args)
    dataset = Custom_VITONHDTrainDataset(args)

    # import matplotlib.pyplot as plt
    # print(dataset.__getitem__(0)['person'].shape)
    # plt.figure(figsize=(10, 5))
    # plt.subplot(1, 2, 1)  
    # plt.imshow(dataset.__getitem__(0)['person'].permute(1, 2, 0).cpu().numpy())
    # plt.title("Person Image")
    # plt.axis('off')
    # plt.subplot(1, 2, 2)
    # plt.imshow(dataset.__getitem__(0)['cloth'].permute(1, 2, 0).cpu().numpy())
    # plt.title("cloth Image")
    # plt.axis('off')
    # plt.show()

    dataloader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True, num_workers=2)
    generator = torch.Generator(device='cuda').manual_seed(args.seed)
    best_loss = float("inf")
    best_epoch = -1
    global_step = 0
    model.train()
    for epoch in tqdm(range(args.num_epochs), desc="Epoch", total=args.num_epochs):
        total_loss = 0.0
        for batch in dataloader:
            # 배치에서 person, cloth, mask 텐서를 device로 이동
            person = batch["person"].to(torch.float16).to(device)
            cloth  = batch["cloth"].to(torch.float16).to(device)
            mask   = batch["mask"].to(torch.float16).to(device)
            
            image = pipeline(
                person,
                cloth,
                mask,
                guidance_scale=args.guidance_scale,
                height=args.height,
                width=args.width,
                generator=generator,
                num_inference_steps = args.num_inference_steps,
            )
            # print("\n person, image :",person.shape, image.shape)
            loss = loss_fn(person, image)
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            total_loss += loss.item()
            global_step += 1
            # wandb.log({"train_loss": loss.item(), "global_step": global_step})
        avg_loss = total_loss / len(dataloader)
        print(f"Epoch [{epoch+1}/{args.num_epochs}], Loss: {avg_loss:.4f}")

        if avg_loss < best_loss:
            best_loss = avg_loss
            best_epoch = epoch + 1

            os.makedirs(args.output_dir, exist_ok=True)
            checkpoint_path = os.path.join(args.output_dir, f"best_model_{best_epoch}.pt")
            torch.save(model.state_dict(), checkpoint_path)
            print(f"새로운 베스트 모델 저장: {checkpoint_path} (Epoch {best_epoch})")
            # wandb에 베스트 모델 업데이트 기록
            # wandb.run.summary["best_loss"] = best_loss
            # wandb.run.summary["best_epoch"] = best_epoch

    print("학습 완료.")
    # wandb.finish()

if __name__ == "__main__":
    main()